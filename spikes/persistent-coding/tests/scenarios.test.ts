import assert from "node:assert/strict";
import { spawn, type ChildProcess } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { FIXED_SOURCE, initRun, verify, type RunConfig } from "../common.ts";

const cwd = dirname(dirname(fileURLToPath(import.meta.url)));
const results: object[] = [];
const candidates: RunConfig["candidate"][] = [
  "durable",
  ...(process.env.DBOS_SYSTEM_DATABASE_URL ? ["dbos" as const] : []),
];
type Exit = {
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
};

function start(candidate: string, command: string, root: string, fault = "") {
  const child = spawn(
    process.execPath,
    ["--experimental-strip-types", `${candidate}.ts`, command, root],
    {
      cwd,
      env: { ...process.env, RL_SPIKE_FAULT: fault },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stdout = "",
    stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const done = new Promise<Exit>((resolve, reject) => {
    child.once("error", reject);
    child.once("close", (code, signal) =>
      resolve({ code, signal, stdout, stderr }),
    );
  });
  return { child, done };
}

function events(root: string): { event: string; [key: string]: unknown }[] {
  const path = join(root, "trace.jsonl");
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8")
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function counts(root: string) {
  const entries = events(root);
  return {
    requests: entries.filter((row) => row.event === "model.request").length,
    tools: entries.filter((row) => row.event === "tool.start").length,
    bash: entries.filter(
      (row) => row.event === "tool.start" && row.name === "bash",
    ).length,
  };
}

async function waitFor(
  child: ChildProcess,
  predicate: () => boolean,
  label: string,
) {
  const until = Date.now() + 20_000;
  while (!predicate()) {
    assert.equal(child.exitCode, null, `Host exited waiting for ${label}`);
    assert.equal(
      child.signalCode,
      null,
      `Host was killed waiting for ${label}`,
    );
    assert.ok(Date.now() < until, `Timed out waiting for ${label}`);
    await delay(20);
  }
}

async function run(candidate: string, root: string) {
  const execution = start(candidate, "run", root);
  const timer = setTimeout(() => execution.child.kill("SIGKILL"), 25_000);
  try {
    return await execution.done;
  } finally {
    clearTimeout(timer);
  }
}

for (const candidate of candidates) {
  await test(
    `${candidate}: real files, failing test feedback, completed restart`,
    { timeout: 60_000 },
    async () => {
      const root = join(mkdtempSync(join(tmpdir(), "rl95-normal-")), "run");
      const config = initRun(root, candidate);
      const began = Date.now();
      const first = await run(candidate, root);
      assert.equal(first.code, 0, first.stderr);
      assert.equal(
        readFileSync(join(config.workspace, "app.py"), "utf8"),
        FIXED_SOURCE,
      );
      assert.equal(verify(config).passed, true);
      const before = counts(root);
      assert.deepEqual(before, { requests: 7, tools: 6, bash: 2 });
      assert.ok(
        events(root).some(
          (row) =>
            row.event === "synthetic.feedback" &&
            row.turn === 4 &&
            row.previousToolFailed === true,
        ),
      );
      const again = await run(candidate, root);
      assert.equal(again.code, 0, again.stderr);
      assert.deepEqual(
        counts(root),
        before,
        "Completed restart must not call the model or tools again",
      );
      results.push({
        candidate,
        case: "normal/completed restart",
        ...before,
        restartRequests: 0,
        restartTools: 0,
        elapsedMs: Date.now() - began,
      });
    },
  );

  for (const fault of ["model", "edit", "bash", "ack"]) {
    await test(
      `${candidate}: ${fault} interruption and same-work restart`,
      { timeout: 60_000 },
      async (t) => {
        const root = join(mkdtempSync(join(tmpdir(), `rl95-${fault}-`)), "run");
        const config = initRun(root, candidate);
        const began = Date.now();
        const execution = start(candidate, "run", root, fault);
        t.after(() => {
          if (
            execution.child.exitCode === null &&
            execution.child.signalCode === null
          )
            execution.child.kill("SIGKILL");
        });
        if (fault === "model") {
          await waitFor(
            execution.child,
            () =>
              events(root).some((row) => row.event === "fault.model.waiting"),
            "in-flight model",
          );
          execution.child.kill("SIGKILL");
        } else if (fault === "bash") {
          await waitFor(
            execution.child,
            () => existsSync(join(config.workspace, "effect.log")),
            "non-idempotent bash effect",
          );
          execution.child.kill("SIGKILL");
        }
        const killed = await execution.done;
        assert.equal(killed.signal, "SIGKILL", killed.stderr);
        const before = counts(root);
        const resumed = await run(candidate, root);
        if (candidate === "durable" || fault === "ack") {
          assert.equal(resumed.code, 0, resumed.stderr);
          assert.equal(verify(config).passed, true);
        } else {
          assert.equal(resumed.code, 1, resumed.stdout);
          assert.match(resumed.stderr, /MANUAL_RECONCILIATION_REQUIRED/);
          assert.deepEqual(
            counts(root),
            before,
            "An unknown Pi episode must never be re-prompted",
          );
        }
        if (fault === "ack")
          assert.deepEqual(
            counts(root),
            before,
            "Lost outer ACK must reuse the completed conversation",
          );
        if (fault === "bash") {
          // The original process may outlive a killed host. Observe its bounded end.
          await delay(2200);
          assert.equal(
            readFileSync(join(config.workspace, "effect.log"), "utf8"),
            "effect\n",
          );
        }
        results.push({
          candidate,
          case: `${fault} interruption`,
          before,
          after: counts(root),
          resume: resumed.code === 0 ? "completed" : "manual reconciliation",
          elapsedMs: Date.now() - began,
        });
      },
    );
  }

  for (const reason of ["STOP", "deadline"]) {
    await test(
      `${candidate}: ${reason} cancels a live model without refreshing authorization`,
      { timeout: 60_000 },
      async (t) => {
        const root = join(mkdtempSync(join(tmpdir(), "rl95-stop-")), "run");
        const config = initRun(
          root,
          candidate,
          reason === "deadline" ? { deadline: Date.now() + 3000 } : {},
        );
        const execution = start(candidate, "run", root, "model");
        t.after(() => {
          if (
            execution.child.exitCode === null &&
            execution.child.signalCode === null
          )
            execution.child.kill("SIGKILL");
        });
        await waitFor(
          execution.child,
          () => events(root).some((row) => row.event === "fault.model.waiting"),
          "live model before cancellation",
        );
        if (reason === "STOP") {
          const cancel = await start(candidate, "cancel", root).done;
          assert.equal(cancel.code, 0, cancel.stderr);
        }
        const timer = setTimeout(() => execution.child.kill("SIGKILL"), 15_000);
        const stopped = await execution.done;
        clearTimeout(timer);
        assert.notEqual(stopped.signal, "SIGKILL", stopped.stderr);
        assert.equal(readRunDeadline(root), config.deadline);
        const before = counts(root);
        assert.equal(before.requests, 1);
        const again = await run(candidate, root);
        assert.deepEqual(counts(root), before);
        if (candidate === "durable") assert.match(again.stdout, /aborted/);
        else assert.equal(again.code, 1);
        results.push({
          candidate,
          case: reason,
          ...counts(root),
          originalDeadlinePreserved: true,
          restartedCode: again.code,
        });
      },
    );
  }

  await test(
    `${candidate}: missing workspace is not replaced`,
    { timeout: 20_000 },
    async () => {
      const root = join(mkdtempSync(join(tmpdir(), "rl95-missing-")), "run");
      const config = initRun(root, candidate);
      renameSync(config.workspace, config.workspace + ".lost");
      const failed = await run(candidate, root);
      assert.equal(failed.code, 1);
      assert.equal(existsSync(config.workspace), false);
      assert.equal(counts(root).requests, 0);
      results.push({
        candidate,
        case: "missing workspace",
        requests: 0,
        replaced: false,
      });
    },
  );

  await test(
    `${candidate}: an untrue model success claim fails independent verification`,
    { timeout: 25_000 },
    async () => {
      const root = join(
        mkdtempSync(join(tmpdir(), "rl95-false-success-")),
        "run",
      );
      initRun(root, candidate);
      const execution = await start(candidate, "run", root, "false-success")
        .done;
      assert.equal(execution.code, 1, execution.stdout);
      const receipt = events(root).find((row) => row.event === "verification");
      assert.equal(receipt?.passed, false);
      results.push({
        candidate,
        case: "untrue model success",
        independentVerificationPassed: false,
        exitCode: execution.code,
      });
    },
  );

  await test(
    `${candidate}: SIGTERM closes the host; restart follows the stored episode`,
    { timeout: 60_000 },
    async (t) => {
      const root = join(mkdtempSync(join(tmpdir(), "rl95-close-")), "run");
      const config = initRun(root, candidate);
      const execution = start(candidate, "run", root, "model");
      t.after(() => {
        if (
          execution.child.exitCode === null &&
          execution.child.signalCode === null
        )
          execution.child.kill("SIGKILL");
      });
      await waitFor(
        execution.child,
        () => events(root).some((row) => row.event === "fault.model.waiting"),
        "model before normal host shutdown",
      );
      execution.child.kill("SIGTERM");
      const timer = setTimeout(() => execution.child.kill("SIGKILL"), 10_000);
      const stopped = await execution.done;
      clearTimeout(timer);
      assert.notEqual(stopped.signal, "SIGKILL", stopped.stderr);
      const before = counts(root);
      const resumed = await run(candidate, root);
      if (candidate === "durable") {
        assert.equal(resumed.code, 0, resumed.stderr);
        assert.equal(verify(config).passed, true);
      } else {
        assert.equal(resumed.code, 1);
        assert.deepEqual(counts(root), before);
      }
      results.push({
        candidate,
        case: "SIGTERM host shutdown",
        before,
        after: counts(root),
        resume: resumed.code === 0 ? "completed" : "manual reconciliation",
      });
    },
  );
}

function readRunDeadline(root: string) {
  return JSON.parse(readFileSync(join(root, "run.json"), "utf8")).deadline;
}

if (process.env.RL_SPIKE_REPORT) {
  mkdirSync(dirname(process.env.RL_SPIKE_REPORT), { recursive: true });
  writeFileSync(
    process.env.RL_SPIKE_REPORT,
    JSON.stringify(
      {
        provider: "synthetic faux/coding; real native tools and CPU tests",
        candidates,
        results,
      },
      null,
      2,
    ) + "\n",
  );
}
