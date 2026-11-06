import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { initialize, readRecord } from "../src/files.ts";
import { maintainKnowledge, runStudy, studyStatus } from "../src/engine.ts";
import { bindSubmission, querySubmission } from "../src/experiments.ts";
import type { StudyConfig } from "../src/contract.ts";

const prepare = fileURLToPath(
  new URL("../../benchmarks/cpu_regression/prepare.py", import.meta.url),
);
const native = {
  skip: process.env.RESEARCH_NATIVE_SANDBOX_TEST !== "1",
  timeout: 60_000,
};
function fixture(rounds = 2, seconds = 900) {
  const parent = mkdtempSync(join(tmpdir(), "research-engine-"));
  const prepared = join(parent, "fixture");
  const output = spawnSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      prepare,
      prepared,
      "--provider",
      "faux",
      "--model",
      "workflow",
    ],
    { encoding: "utf8" },
  );
  assert.equal(output.status, 0, output.stderr);
  const config = JSON.parse(
    readFileSync(join(prepared, "study.json"), "utf8"),
  ) as StudyConfig;
  config.model.thinking = "off";
  config.maxRounds = rounds;
  config.minimumRounds = rounds;
  config.authorization.slots = config.authorization.slots.slice(0, rounds);
  config.authorization.expiresAt = Date.now() + seconds * 1000;
  const root = initialize(join(parent, "run"), config);
  return {
    root,
    config,
    cleanup: () => rmSync(parent, { recursive: true, force: true }),
  };
}
import { events, scripted } from "./scripted.ts";

test("a configured external experiment adapter cannot fall back or change identity before any model/database admission", async () => {
  const f = fixture();
  try {
    f.config.execution = {
      id: "external-test",
      version: "1",
      identity: "frozen",
    };
    writeFileSync(join(f.root, "study.json"), JSON.stringify(f.config));
    await assert.rejects(runStudy(f.root), /requires its exact configured/);
    await assert.rejects(
      runStudy(f.root, {
        experiments: {
          execution: { ...f.config.execution, identity: "changed" },
          run: async () => {
            throw new Error("must not submit");
          },
          readReceipts: () => {
            throw new Error("must not collect");
          },
        },
      }),
      /does not match the frozen/,
    );
    assert.equal(existsSync(join(f.root, "runtime.sqlite")), false);
    assert.equal(existsSync(join(f.root, "experiments")), false);
  } finally {
    f.cleanup();
  }
});

async function until(predicate: () => boolean) {
  const end = Date.now() + 10_000;
  while (!predicate()) {
    assert.ok(
      Date.now() < end,
      "Timed out waiting for the original job/Manager",
    );
    await delay(10);
  }
}
function controller(
  root: string,
  fault: "cpu-ack" | "manager-ack",
  failManager = false,
) {
  const code = `import {runStudy} from ${JSON.stringify(new URL("../src/engine.ts", import.meta.url).href)}; import {scripted} from ${JSON.stringify(new URL("./scripted.ts", import.meta.url).href)}; const root=process.argv[1]; console.log(JSON.stringify(await runStudy(root,{models:scripted(root,${failManager}).models})));`;
  const child = spawn(
    process.execPath,
    ["--experimental-strip-types", "--input-type=module", "-e", code, root],
    {
      env: {
        ...process.env,
        RL_CPU_FAULT: fault === "cpu-ack" ? "ack" : "",
        RESEARCH_FAULT: fault === "manager-ack" ? "manager-ack" : "",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  child.stderr.on("data", (chunk) => {
    output += chunk;
  });
  const timer = setTimeout(() => child.kill("SIGKILL"), 20_000);
  return {
    child,
    done: new Promise<{
      code: number | null;
      signal: NodeJS.Signals | null;
      output: string;
    }>((resolve, reject) => {
      child.once("error", reject);
      child.once("close", (code, signal) => {
        clearTimeout(timer);
        resolve({ code, signal, output });
      });
    }),
  };
}

test(
  "two real CPU rounds, fresh Review, durable asynchronous Manager and live read",
  native,
  async () => {
    const f = fixture();
    try {
      const script = scripted(f.root);
      const outcome = await runStudy(f.root, { models: script.models });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      const r1 = readRecord(f.root, "r0001"),
        r2 = readRecord(f.root, "r0002");
      assert.equal(r1.receipts[0].valid, true);
      assert.equal(r2.receipts[0].valid, true);
      assert.notEqual(r1.receipts[0].candidate, r2.receipts[0].candidate);
      assert.deepEqual(r2.plan.basedOn, ["r0001"]);
      assert.equal(
        new Set([
          ...Object.values(r1.conversations),
          ...Object.values(r2.conversations),
        ]).size,
        6,
      );
      assert.equal(r2.review.goal, "not-achieved");
      assert.equal(script.observedNotes.length, 2);
      assert.match(script.observedNotes[0], /No reviewed research/);
      assert.match(script.observedNotes[1], /Recorded CPU evidence/);
      const state = await studyStatus(f.root);
      assert.deepEqual(state.state?.pending, []);
      assert.equal(state.state?.publications.length, 2);
      const before = script.faux.state.callCount;
      assert.deepEqual(
        await runStudy(f.root, { models: script.models }),
        outcome,
      );
      assert.equal(
        script.faux.state.callCount,
        before,
        "Completed restart must not ask models again",
      );
      const log = events(f.root);
      const publication = log.findIndex(
        (row) => row.event === "knowledge.published",
      );
      const planner2 = r2.conversations.plan;
      assert.ok(
        log.findIndex(
          (row) => row.event === "role.active" && row.conversation === planner2,
        ) < publication,
      );
      assert.ok(
        log.findIndex(
          (row) =>
            row.event === "role.finished" && row.conversation === planner2,
        ) > publication,
      );
    } finally {
      f.cleanup();
    }
  },
);

test(
  "failed Manager preserves old notes, authoritative Reviews and direct R1 feedback",
  native,
  async () => {
    const f = fixture();
    try {
      const original = readFileSync(
        join(f.root, "knowledge", "research.md"),
        "utf8",
      );
      const script = scripted(f.root, true);
      const outcome = await runStudy(f.root, { models: script.models });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      assert.equal(
        readFileSync(join(f.root, "knowledge", "research.md"), "utf8"),
        original,
      );
      assert.deepEqual(readRecord(f.root, "r0002").plan.basedOn, ["r0001"]);
      const status = await studyStatus(f.root);
      assert.deepEqual(status.state?.records, ["r0001", "r0002"]);
      assert.deepEqual(status.state?.pending, ["r0001", "r0002"]);
      assert.equal(status.state?.publications.length, 0);
    } finally {
      f.cleanup();
    }
  },
);

test("final-only feedback is rejected before a learning role can start", () => {
  const f = fixture();
  try {
    assert.throws(
      () =>
        initialize(join(f.root, "nested"), {
          ...f.config,
          inputs: { ...f.config.inputs, feedback: "final-only" },
        }),
      /final-only/,
    );
  } finally {
    f.cleanup();
  }
});

for (const mode of ["duplicate", "mixed-error", "symlink"] as const) {
  test(
    `Manager ${mode} cannot publish twice or expose an unsuccessful draft`,
    native,
    async () => {
      const f = fixture();
      try {
        const original = readFileSync(
          join(f.root, "knowledge", "research.md"),
          "utf8",
        );
        writeFileSync(
          join(f.root, "synthetic-secret"),
          "SYNTHETIC PRIVATE VALUE\n",
        );
        const script = scripted(f.root, mode);
        const outcome = await runStudy(f.root, { models: script.models });
        assert.equal(outcome.status, "completed", JSON.stringify(outcome));
        const status = await studyStatus(f.root);
        if (mode === "duplicate") {
          assert.equal(status.state?.publications.length, 2);
          assert.equal(
            new Set(status.state!.publications.map((row) => row.id)).size,
            2,
          );
        } else {
          assert.equal(
            readFileSync(join(f.root, "knowledge", "research.md"), "utf8"),
            original,
          );
          assert.deepEqual(status.state?.pending, ["r0001", "r0002"]);
        }
        assert.equal(
          readFileSync(join(f.root, "synthetic-secret"), "utf8"),
          "SYNTHETIC PRIVATE VALUE\n",
        );
      } finally {
        f.cleanup();
      }
    },
  );
}

test(
  "ACK-loss resume quiesces pending Execute and reviews the bound original job without another send",
  native,
  async () => {
    const f = fixture(1);
    try {
      const crashed = await controller(f.root, "cpu-ack", true).done;
      assert.equal(crashed.signal, "SIGKILL", crashed.output);
      await until(
        () =>
          querySubmission(f.root, f.config, "round-1").state === "completed",
      );
      const unbound = scripted(f.root, true);
      await assert.rejects(
        runStudy(f.root, { resume: true, models: unbound.models }),
        /Collect the bound original job/,
      );
      assert.equal(unbound.faux.state.callCount, 0);
      const original = querySubmission(f.root, f.config, "round-1");
      assert.ok(original.jobId);
      const bound = await bindSubmission(
        f.root,
        f.config,
        "round-1",
        original.jobId,
      );
      assert.ok(!("state" in bound));
      const before = events(f.root).length;
      const resumed = await runStudy(f.root, {
        resume: true,
        models: scripted(f.root, true).models,
      });
      assert.equal(resumed.status, "completed", JSON.stringify(resumed));
      const fresh = events(f.root)
        .slice(before)
        .filter((row) => row.event === "role.active");
      assert.equal(fresh[0].role, "review");
      assert.equal(
        fresh.some((row) => row.role === "execute"),
        false,
      );
      const receipt = readRecord(f.root, "r0001").receipts[0];
      const starts = readFileSync(
        join(receipt.rawResult, "..", "job.log"),
        "utf8",
      )
        .split("\n")
        .filter((line) => line.includes('"event":"cpu-job.started"')).length;
      assert.equal(starts, 1);
      assert.equal(
        readFileSync(join(f.root, "study.json"), "utf8").includes(
          String(f.config.authorization.expiresAt),
        ),
        true,
      );
    } finally {
      f.cleanup();
    }
  },
);

test(
  "Manager restart after rename reuses frozen bytes and the same publication identity",
  native,
  async () => {
    const f = fixture();
    try {
      const crashed = await controller(f.root, "manager-ack").done;
      assert.equal(crashed.signal, "SIGKILL", crashed.output);
      const header = readFileSync(
        join(f.root, "knowledge", "research.md"),
        "utf8",
      ).split("\n")[0];
      const id = header.match(/^<!-- publication:([0-9]+) -->$/)?.[1];
      assert.ok(id);
      const resumed = await runStudy(f.root, {
        models: scripted(f.root).models,
      });
      assert.equal(resumed.status, "completed", JSON.stringify(resumed));
      const status = await studyStatus(f.root);
      assert.equal(
        status.state?.publications.filter((row) => row.id === id).length,
        1,
      );
      assert.equal(status.state?.publications.length, 2);
      assert.deepEqual(status.state?.pending, []);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "maintain cannot refresh the original model authorization by editing the config",
  native,
  async () => {
    const f = fixture(1);
    try {
      await runStudy(f.root, { models: scripted(f.root, true).models });
      writeFileSync(
        join(f.root, "study.json"),
        JSON.stringify({
          ...f.config,
          authorization: {
            ...f.config.authorization,
            expiresAt: f.config.authorization.expiresAt + 3600_000,
          },
        }),
      );
      const blocked = scripted(f.root, "hang");
      await assert.rejects(
        maintainKnowledge(f.root, { models: blocked.models }),
        /Original Study contract changed/,
      );
      assert.equal(blocked.faux.state.callCount, 0);
    } finally {
      f.cleanup();
    }
  },
);

for (const reason of ["STOP", "deadline"] as const) {
  test(
    `maintain ${reason} aborts an in-flight model using the original authorization`,
    native,
    async () => {
      const f = fixture(1, reason === "deadline" ? 3 : 900);
      try {
        await runStudy(f.root, { models: scripted(f.root, true).models });
        const script = scripted(f.root, "hang");
        const running = maintainKnowledge(f.root, { models: script.models });
        await until(script.managerWaiting);
        if (reason === "STOP")
          writeFileSync(join(f.root, "STOP"), "operator stop\n");
        const outcome = await running;
        assert.equal("status" in outcome ? outcome.status : null, "aborted");
        assert.equal(script.faux.state.callCount, 1);
        assert.deepEqual((await studyStatus(f.root)).state?.pending, ["r0001"]);
        assert.equal(
          JSON.parse(readFileSync(join(f.root, "study.json"), "utf8"))
            .authorization.expiresAt,
          f.config.authorization.expiresAt,
        );
      } finally {
        f.cleanup();
      }
    },
  );
}

test(
  "diagnostic log failure cannot turn a successful atomic publication into a failed Manager",
  native,
  async () => {
    const f = fixture(1);
    try {
      await runStudy(f.root, { models: scripted(f.root, true).models });
      rmSync(join(f.root, "events.jsonl"));
      mkdirSync(join(f.root, "events.jsonl"));
      const outcome = await maintainKnowledge(f.root, {
        models: scripted(f.root, "duplicate").models,
      });
      assert.equal("status" in outcome ? outcome.status : null, "completed");
      const status = await studyStatus(f.root);
      assert.deepEqual(status.state?.pending, []);
      assert.equal(status.state?.publications.length, 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "a premature goal-achieved reply is corrected without another Execute",
  native,
  async () => {
    const f = fixture();
    try {
      const outcome = await runStudy(f.root, {
        models: scripted(f.root, "premature").models,
      });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      assert.equal(readRecord(f.root, "r0001").review.goal, "not-achieved");
      assert.ok(
        events(f.root).some(
          (row) =>
            row.event === "tool.end" &&
            row.name === "submit_review" &&
            row.isError,
        ),
      );
      assert.deepEqual((await studyStatus(f.root)).state?.records, [
        "r0001",
        "r0002",
      ]);
    } finally {
      f.cleanup();
    }
  },
);
