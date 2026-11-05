import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  mkdtempSync,
  readFileSync,
  readlinkSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { Receipt, StudyConfig } from "../src/contract.ts";
import {
  bindSubmission,
  collectSubmission,
  querySubmission,
  readReceipts,
  runExperiment,
} from "../src/experiments.ts";

const prepare = fileURLToPath(
  new URL("../../benchmarks/cpu_regression/prepare.py", import.meta.url),
);
function fixture() {
  const parent = mkdtempSync(join(tmpdir(), "research-cpu-test-"));
  const root = join(parent, "study");
  const prepared = spawnSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      prepare,
      root,
      "--provider",
      "not-called",
      "--model",
      "not-called",
    ],
    {
      encoding: "utf8",
      env: { ...process.env, UV_CACHE_DIR: "/private/tmp/rl96-uv-cache" },
    },
  );
  assert.equal(prepared.status, 0, prepared.stderr);
  const config = JSON.parse(
    readFileSync(join(root, "study.json"), "utf8"),
  ) as StudyConfig;
  return {
    root,
    config,
    cleanup: () => rmSync(parent, { recursive: true, force: true }),
  };
}
const native = { skip: process.platform !== "darwin", timeout: 60_000 };
function sendCount(receipt: Receipt): number {
  return readFileSync(join(receipt.rawResult, "..", "job.log"), "utf8")
    .split("\n")
    .filter((line) => line.includes('"event":"cpu-job.started"')).length;
}

test("final-only inputs/evaluation reject before candidate access or physical submission", async () => {
  const f = fixture();
  try {
    for (const boundary of ["inputs", "evaluation"] as const) {
      const config = structuredClone(f.config);
      config[boundary].feedback = "final-only";
      await assert.rejects(
        runExperiment(f.root, config, "/missing-candidate", "round-1"),
        /Final-only/,
      );
      assert.throws(() => readReceipts(f.root, config), /Final-only/);
      assert.throws(
        () => querySubmission(f.root, config, "round-1"),
        /Final-only/,
      );
      await assert.rejects(
        bindSubmission(f.root, config, "round-1", "invented"),
        /Final-only/,
      );
      assert.equal(existsSync(join(f.root, "experiments")), false);
    }
  } finally {
    f.cleanup();
  }
});

test("STOP and original expiry prevent new local CPU jobs", async () => {
  const f = fixture();
  try {
    writeFileSync(join(f.root, "STOP"), "operator stop\n");
    await assert.rejects(
      runExperiment(f.root, f.config, f.config.source.root, "round-1"),
      /STOP/,
    );
    rmSync(join(f.root, "STOP"));
    await assert.rejects(
      runExperiment(
        f.root,
        {
          ...f.config,
          authorization: {
            ...f.config.authorization,
            expiresAt: Date.now() - 1,
          },
        },
        f.config.source.root,
        "round-1",
      ),
      /expired/,
    );
    assert.equal(existsSync(join(f.root, "experiments")), false);
  } finally {
    f.cleanup();
  }
});

test(
  "real local CPU snapshot and trusted scorer ignore candidate-written fake scores",
  native,
  async () => {
    const f = fixture();
    try {
      const source = join(f.config.source.root, "candidate.py");
      const original = readFileSync(source, "utf8");
      writeFileSync(
        source,
        'print(\'{"metrics":{"r2":999999}}\')\n' + original,
      );
      const first = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-1",
      );
      assert.equal(
        first.executionComplete,
        true,
        readFileSync(first.rawResult, "utf8"),
      );
      assert.equal(first.valid, true, readFileSync(first.rawResult, "utf8"));
      assert.ok(Number.isFinite(first.metrics.r2));
      assert.notEqual(first.metrics.r2, 999999);
      assert.equal(first.inputVersion, f.config.inputs.version);
      assert.equal(first.evaluationVersion, f.config.evaluation.version);
      assert.equal(sendCount(first), 1);
      assert.match(
        readFileSync(
          join(first.rawResult, "..", "prediction-output.txt"),
          "utf8",
        ),
        /999999/,
      );
      assert.deepEqual(
        await runExperiment(f.root, f.config, f.config.source.root, "round-1"),
        first,
      );
      assert.equal(sendCount(first), 1);
      assert.deepEqual(readReceipts(f.root, f.config), [first]);
      assert.deepEqual(
        await collectSubmission(f.root, f.config, "round-1"),
        first,
      );
      writeFileSync(source, original + "\n# different candidate\n");
      await assert.rejects(
        runExperiment(f.root, f.config, f.config.source.root, "round-1"),
        /different candidate/,
      );
      assert.equal(
        readFileSync(join(first.candidatePath, "candidate.py"), "utf8"),
        'print(\'{"metrics":{"r2":999999}}\')\n' + original,
      );
      const changed = structuredClone(f.config);
      changed.evaluation.version = "different";
      await assert.rejects(
        runExperiment(f.root, changed, f.config.source.root, "round-1"),
        /different input\/evaluation\/configuration/,
      );
      changed.evaluation.version = f.config.evaluation.version;
      changed.inputs.version = "different";
      assert.throws(
        () => querySubmission(f.root, changed, "round-1"),
        /different input\/evaluation\/configuration/,
      );
      assert.equal(sendCount(first), 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "a second round cannot hand the host scorer a symlink to the first round's private predictions",
  native,
  async () => {
    const f = fixture();
    try {
      const first = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-1",
      );
      assert.equal(first.valid, true);
      const oldPrediction = join(
        first.rawResult,
        "..",
        "sealed",
        "predictions.json",
      );
      const originalBytes = readFileSync(oldPrediction);
      writeFileSync(
        join(f.config.source.root, "predict.py"),
        `import os,sys\nos.symlink(${JSON.stringify(oldPrediction)},sys.argv[2])\n`,
      );
      const second = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-2",
      );
      const output = join(
        second.rawResult,
        "..",
        "predictions",
        "predictions.json",
      );
      assert.equal(lstatSync(output).isSymbolicLink(), true);
      assert.equal(readlinkSync(output), oldPrediction);
      assert.equal(second.valid, false);
      assert.deepEqual(second.metrics, {});
      const raw = JSON.parse(readFileSync(second.rawResult, "utf8"));
      assert.equal(raw.predictionExitCode, 0);
      assert.equal(raw.scoringExitCode, null);
      assert.equal(
        existsSync(join(second.rawResult, "..", "sealed", "predictions.json")),
        false,
      );
      assert.deepEqual(readFileSync(oldPrediction), originalBytes);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "prediction intake rejects FIFO without waiting and rejects oversized regular files",
  native,
  async () => {
    const f = fixture();
    try {
      for (const [slot, code] of [
        ["round-1", "import os,sys\nos.mkfifo(sys.argv[2])\n"],
        [
          "round-2",
          "import sys\nwith open(sys.argv[2],'wb') as f: f.truncate(1048577)\n",
        ],
      ]) {
        writeFileSync(join(f.config.source.root, "predict.py"), code);
        const started = Date.now();
        const receipt = await runExperiment(
          f.root,
          f.config,
          f.config.source.root,
          slot,
        );
        assert.equal(receipt.valid, false);
        assert.ok(
          Date.now() - started < 5000,
          "Special-file intake must not block waiting for a FIFO writer",
        );
        const raw = JSON.parse(readFileSync(receipt.rawResult, "utf8"));
        assert.equal(raw.predictionExitCode, 0);
        assert.equal(raw.scoringExitCode, null);
        assert.equal(
          existsSync(
            join(receipt.rawResult, "..", "sealed", "predictions.json"),
          ),
          false,
        );
      }
    } finally {
      f.cleanup();
    }
  },
);

test(
  "cached receipts must match the frozen intent, original backend result and feedback role",
  native,
  async () => {
    const f = fixture();
    try {
      const first = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-1",
      );
      assert.equal(first.valid, true);
      const cache = join(first.rawResult, "..", "receipt.json");
      for (const mutation of [
        { candidate: "other-candidate" },
        { evaluation: "other-evaluation" },
        { evaluationVersion: "other-version" },
        { inputVersion: "other-input" },
        { feedback: "final-only" },
        { slot: "round-2" },
        { authorization: "other-authorization" },
        { jobId: "other-job" },
        { rawResult: "/path/to/other-result" },
        { candidatePath: "/path/to/other-candidate" },
        { valid: false },
        { executionComplete: false },
        { metrics: { ...first.metrics, r2: 999999 } },
      ]) {
        writeFileSync(cache, JSON.stringify({ ...first, ...mutation }));
        assert.throws(() => readReceipts(f.root, f.config), /Cached receipt/);
        await assert.rejects(
          collectSubmission(f.root, f.config, "round-1"),
          /Cached receipt/,
        );
      }
      writeFileSync(cache, JSON.stringify(first));
      assert.deepEqual(readReceipts(f.root, f.config), [first]);
      assert.equal(sendCount(first), 1);
      // Simulate a retained start record whose terminal artifact was lost. A PID
      // in old metadata proves neither current liveness nor successful completion.
      rmSync(cache);
      renameSync(first.rawResult, first.rawResult + ".held");
      assert.equal(
        querySubmission(f.root, f.config, "round-1").state,
        "last-confirmed-started",
      );
      const pending = await collectSubmission(f.root, f.config, "round-1");
      assert.equal(
        "state" in pending && pending.state,
        "last-confirmed-started",
      );
      const intentPath = join(first.rawResult, "..", "intent.json");
      const intent = JSON.parse(readFileSync(intentPath, "utf8"));
      intent.deadline = Date.now() - 1;
      writeFileSync(intentPath, JSON.stringify(intent));
      assert.equal(
        querySubmission(f.root, f.config, "round-1").state,
        "unknown",
      );
      const unknown = await collectSubmission(f.root, f.config, "round-1");
      assert.equal("state" in unknown && unknown.state, "unknown");
      assert.equal(sendCount(first), 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "trusted snapshot cannot follow an intermediate candidate symlink into host-private files",
  native,
  async () => {
    const f = fixture();
    try {
      writeFileSync(
        join(f.root, "private", "candidate.py"),
        "synthetic-private-value\n",
      );
      symlinkSync(
        join(f.root, "private"),
        join(f.config.source.root, "nested"),
        "dir",
      );
      f.config.source.files = ["nested/candidate.py"];
      await assert.rejects(
        runExperiment(f.root, f.config, f.config.source.root, "round-1"),
      );
      assert.equal(existsSync(join(f.root, "experiments")), false);
      assert.equal(existsSync(join(f.root, "candidates")), false);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "ACK loss survives a new caller: one actual job, verified bind and collect only",
  native,
  async () => {
    const f = fixture();
    try {
      const script = `import {readFileSync} from 'node:fs'; import {join} from 'node:path'; import {runExperiment} from ${JSON.stringify(new URL("../src/experiments.ts", import.meta.url).href)}; const root=process.argv[1]; const config=JSON.parse(readFileSync(join(root,'study.json'),'utf8')); await runExperiment(root,config,config.source.root,'round-1');`;
      const host = spawn(
        process.execPath,
        [
          "--experimental-strip-types",
          "--input-type=module",
          "-e",
          script,
          f.root,
        ],
        {
          env: { ...process.env, RL_CPU_FAULT: "ack" },
          stdio: "ignore",
        },
      );
      const exit = await new Promise<{
        code: number | null;
        signal: NodeJS.Signals | null;
      }>((resolve, reject) => {
        host.once("error", reject);
        host.once("exit", (code, signal) => resolve({ code, signal }));
      });
      assert.equal(exit.signal, "SIGKILL");
      const queried = querySubmission(f.root, f.config, "round-1");
      assert.ok(queried.jobId);
      await assert.rejects(
        runExperiment(f.root, f.config, f.config.source.root, "round-1"),
        /UNKNOWN_SUBMISSION/,
      );
      await assert.rejects(
        bindSubmission(f.root, f.config, "round-1", "invented-job-id"),
        /not the original backend job/,
      );
      writeFileSync(
        join(f.root, "STOP"),
        "stop new launches; allow collection\n",
      );
      let collected = await bindSubmission(
        f.root,
        f.config,
        "round-1",
        queried.jobId,
      );
      const until = Date.now() + 20_000;
      while ("state" in collected && Date.now() < until) {
        await new Promise((resolve) => setTimeout(resolve, 50));
        collected = await collectSubmission(f.root, f.config, "round-1");
      }
      assert.ok(
        !("state" in collected),
        "Original physical job did not publish a terminal result",
      );
      assert.equal(collected.jobId, queried.jobId);
      assert.equal(
        collected.valid,
        true,
        readFileSync(collected.rawResult, "utf8"),
      );
      assert.equal(sendCount(collected), 1);
      assert.deepEqual(
        await runExperiment(f.root, f.config, f.config.source.root, "round-1"),
        collected,
      );
      assert.equal(sendCount(collected), 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "qualitative evaluation accepts observations with empty metrics and no ranking",
  native,
  async () => {
    const f = fixture();
    try {
      const observations = [
        "The prediction artifact was produced; this fixed qualitative protocol defines no numeric ranking",
      ];
      const command = [
        f.config.python,
        "-I",
        "-B",
        "-c",
        `import json; print(json.dumps(${JSON.stringify({ metrics: {}, observations })}))`,
      ];
      f.config.evaluation = {
        ...f.config.evaluation,
        id: "qualitative-development",
        version: "qualitative-1",
        description:
          "Fixed host observations only; no scalar metric or ranking is defined",
        command,
      };
      const receipt = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-1",
      );
      assert.equal(receipt.executionComplete, true);
      assert.equal(receipt.valid, true);
      assert.deepEqual(receipt.metrics, {});
      assert.deepEqual(receipt.observations, observations);
      assert.equal(receipt.evaluationVersion, "qualitative-1");
      assert.deepEqual(readReceipts(f.root, f.config), [receipt]);
      const intent = JSON.parse(
        readFileSync(join(receipt.rawResult, "..", "intent.json"), "utf8"),
      );
      assert.deepEqual(intent.config.evaluation.command, command);
      assert.equal(sendCount(receipt), 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "an expired original authorization still permits collection, never a new slot",
  native,
  async () => {
    const f = fixture();
    try {
      f.config.authorization.expiresAt = Date.now() + 3000;
      const first = await runExperiment(
        f.root,
        f.config,
        f.config.source.root,
        "round-1",
      );
      assert.equal(first.valid, true, readFileSync(first.rawResult, "utf8"));
      await new Promise((resolve) =>
        setTimeout(
          resolve,
          Math.max(0, f.config.authorization.expiresAt - Date.now() + 10),
        ),
      );
      writeFileSync(join(f.root, "STOP"), "collection only\n");
      assert.deepEqual(
        await bindSubmission(f.root, f.config, "round-1", first.jobId),
        first,
      );
      assert.deepEqual(
        await runExperiment(f.root, f.config, f.config.source.root, "round-1"),
        first,
      );
      assert.equal(sendCount(first), 1);
      rmSync(join(f.root, "STOP"));
      await assert.rejects(
        runExperiment(f.root, f.config, f.config.source.root, "round-2"),
        /expired/,
      );
      assert.equal(sendCount(first), 1);
    } finally {
      f.cleanup();
    }
  },
);
