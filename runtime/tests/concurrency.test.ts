import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import test from "node:test";
import { createModels } from "@earendil-works/pi-ai/models";
import {
  fauxAssistantMessage,
  fauxProvider,
  fauxToolCall,
  type FauxResponseFactory,
} from "@earendil-works/pi-ai/providers/faux";
import type {
  ExperimentBackend,
  Receipt,
  StudyConfig,
} from "../src/contract.ts";
import { initialize, readRecord, validateConfig } from "../src/files.ts";
import { openStudy, runStudy, studyStatus } from "../src/engine.ts";
import { events } from "./scripted.ts";

const native = {
  skip: process.env.RESEARCH_NATIVE_SANDBOX_TEST !== "1",
  timeout: 60_000,
};
function fixture(experiments = 1, rounds = 2) {
  const parent = mkdtempSync(join(tmpdir(), "research-concurrent-"));
  const prepared = join(parent, "fixture");
  const result = spawnSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      "benchmarks/cpu_regression/prepare.py",
      prepared,
      "--provider",
      "faux",
      "--model",
      "workflow",
    ],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const config = JSON.parse(
    readFileSync(join(prepared, "study.json"), "utf8"),
  ) as StudyConfig;
  config.model.thinking = "off";
  config.maxRounds = rounds;
  config.minimumRounds = rounds;
  config.authorization.slots = Array.from(
    { length: rounds },
    (_, i) => `round-${i + 1}`,
  );
  config.authorization.expiresAt = Date.now() + 900_000;
  config.concurrency = { investigations: 2, experiments };
  config.execution = {
    id: "synthetic-concurrency",
    version: "1",
    identity: "fixed",
  };
  config.tasks = [1, 2].map((i) => ({
    id: `human-${i}`,
    source: "human",
    question: `Independent ${i}`,
    constraints: [`Keep constraint ${i}`],
    basedOn: [],
  }));
  const root = initialize(join(parent, "run"), config);
  return {
    root,
    config,
    cleanup: () => rmSync(parent, { recursive: true, force: true }),
  };
}
function modelsFor(
  root: string,
  options: {
    overlap?: boolean;
    goal?: boolean;
  } = {},
) {
  const provider = fauxProvider({
    models: [{ id: "workflow", reasoning: false }],
  });
  const models = createModels();
  models.setProvider(provider.provider);
  let plans = 0,
    managerActive = 0,
    maxManagers = 0;
  const baselines: string[] = [];
  const respond: FauxResponseFactory = async (ctx) => {
    const user = ctx.messages.find((message) => message.role === "user")!;
    const text =
      typeof user.content === "string"
        ? user.content
        : JSON.stringify(user.content);
    const feedback = ctx.messages.filter(
      (message) => message.role === "toolResult",
    );
    const call = (name: string, args: Parameters<typeof fauxToolCall>[1]) =>
      fauxAssistantMessage(fauxToolCall(name, args), { stopReason: "toolUse" });
    if (text.startsWith("Read the latest published knowledge")) {
      managerActive++;
      maxManagers = Math.max(maxManagers, managerActive);
      await delay(40);
      managerActive--;
      return call("publish_notes", {});
    }
    if (text.startsWith("You are an independent fresh Review")) {
      assert.equal(
        JSON.stringify(ctx.messages).includes("PRIVATE_EXECUTE_CHAT"),
        false,
      );
      return call("submit_review", {
        interpretation: "Synthetic terminal evidence",
        limitations: ["Synthetic adapter"],
        knowledgeChanged: true,
        goal:
          options.goal && text.includes("Current measured round: 2;")
            ? "achieved"
            : "not-achieved",
        continue: !(
          options.goal && text.includes("Current measured round: 2;")
        ),
        nextQuestion: "Use reviewed evidence",
      });
    }
    if (text.includes("\nImmutable human constraints:")) {
      if (feedback.length === 0)
        return call("write", {
          path: "candidate.py",
          content: "# PRIVATE_EXECUTE_CHAT\nvalue = 1\n",
        });
      if (feedback.length === 1) return call("run_experiment", {});
      return call("finish_execute", {
        summary: "Actual synthetic backend execution",
      });
    }
    if (feedback.length === 0) {
      plans++;
      if (options.overlap) {
        const until = Date.now() + 10_000;
        while (plans < 2) {
          assert.ok(Date.now() < until, "Independent planners must overlap");
          await delay(10);
        }
      }
      return call("read", { path: "candidate.py" });
    }
    baselines.push(JSON.stringify(feedback.at(-1)?.content));
    const task = JSON.parse(
      text.split("\nTask: ")[1].split("\nCandidate files:")[0],
    );
    return call("submit_plan", {
      question: task.question,
      change: "Small synthetic change",
      rationale: "Test independent durable work",
      basedOn: task.basedOn,
    });
  };
  provider.setResponses(Array.from({ length: 100 }, () => respond));
  return { models, provider, baselines, maxManagers: () => maxManagers };
}
function backend(f: ReturnType<typeof fixture>, unknown = false) {
  const receipts: Receipt[] = [];
  const starts: string[] = [];
  let active = 0,
    maxActive = 0,
    failures = unknown ? 1 : 0;
  function settle(
    slot: string,
    workspace = join(
      f.root,
      "workspaces",
      slot === "round-1" ? "r0001" : "r0002",
    ),
  ) {
    const rawResult = join(f.root, `result-${slot}.json`);
    writeFileSync(rawResult, '{"synthetic":true}\n');
    const receipt: Receipt = {
      id: slot,
      authorization: f.config.authorization.id,
      slot,
      candidate: slot,
      inputVersion: f.config.inputs.version,
      evaluation: f.config.evaluation.id,
      evaluationVersion: f.config.evaluation.version,
      feedback: "development",
      jobId: `synthetic:${slot}`,
      executionComplete: true,
      valid: true,
      metrics: { score: 1 },
      observations: ["Synthetic"],
      rawResult,
      candidatePath: workspace,
    };
    receipts.push(receipt);
    return receipt;
  }
  const experiments: ExperimentBackend = {
    execution: f.config.execution!,
    readReceipts: () => [...receipts],
    run: async (_root, _config, workspace, slot) => {
      starts.push(slot);
      active++;
      maxActive = Math.max(maxActive, active);
      await delay(slot === "round-1" ? 300 : 40);
      active--;
      if (failures > 0) {
        failures--;
        throw new Error("UNKNOWN_SUBMISSION: synthetic acknowledgement lost");
      }
      return settle(slot, workspace);
    },
  };
  return {
    experiments,
    starts,
    settle,
    maxActive: () => maxActive,
    failNext: () => failures++,
  };
}

for (const cap of [1, 2])
  test(
    `independent investigations overlap with experiment cap ${cap}, stable IDs and serial Manager`,
    native,
    async () => {
      const f = fixture(cap);
      try {
        const m = modelsFor(f.root, { overlap: true }),
          b = backend(f);
        const outcome = await runStudy(f.root, {
          models: m.models,
          experiments: b.experiments,
        });
        assert.equal(outcome.status, "completed", JSON.stringify(outcome));
        assert.equal(b.maxActive(), cap);
        assert.equal(m.maxManagers(), 1);
        assert.equal(m.baselines.length, 2);
        assert.equal(
          m.baselines[0],
          m.baselines[1],
          "Independent human baseline is original source",
        );
        const first = readRecord(f.root, "r0001"),
          second = readRecord(f.root, "r0002");
        assert.equal(first.task.id, "human-1");
        assert.equal(second.task.id, "human-2");
        assert.equal(first.receipts[0].slot, "round-1");
        assert.equal(second.receipts[0].slot, "round-2");
        assert.equal(
          new Set([
            ...Object.values(first.conversations),
            ...Object.values(second.conversations),
          ]).size,
          6,
        );
        const status = await studyStatus(f.root);
        assert.deepEqual(status.state?.permits, []);
        assert.deepEqual(status.state?.pending, []);
        if (cap === 2)
          assert.deepEqual(status.state?.records, ["r0002", "r0001"]);
        const calls = m.provider.state.callCount;
        assert.deepEqual(
          await runStudy(f.root, {
            models: m.models,
            experiments: b.experiments,
          }),
          outcome,
        );
        assert.equal(m.provider.state.callCount, calls);
        assert.equal(b.starts.length, 2);
      } finally {
        f.cleanup();
      }
    },
  );

test(
  "unknown effect retains capacity and resumes original receipt plus never-submitted sibling",
  native,
  async () => {
    const f = fixture();
    try {
      const m = modelsFor(f.root, { overlap: true }),
        b = backend(f, true);
      const outcome = await runStudy(f.root, {
        models: m.models,
        experiments: b.experiments,
      });
      assert.equal(outcome.status, "faulted", JSON.stringify(outcome));
      assert.equal(b.starts.length, 1);
      const original = b.starts[0];
      assert.deepEqual((await studyStatus(f.root)).state?.permits, [original]);
      const blocked = modelsFor(f.root);
      await assert.rejects(
        runStudy(f.root, {
          resume: true,
          models: blocked.models,
          experiments: b.experiments,
        }),
        /Collect the bound original job/,
      );
      assert.equal(blocked.provider.state.callCount, 0);
      assert.deepEqual(b.starts, [original]);
      const waiting = (await studyStatus(f.root)).state!.investigations.find(
        (row) => f.config.authorization.slots[row.round] !== original,
      )!;
      const previousExecutor = waiting.executor;
      b.settle(original);
      b.failNext();
      const resumed = await runStudy(f.root, {
        resume: true,
        models: modelsFor(f.root).models,
        experiments: b.experiments,
      });
      assert.equal(resumed.status, "faulted", JSON.stringify(resumed));
      const interrupted = (
        await studyStatus(f.root)
      ).state!.investigations.find((row) => row.round === waiting.round)!;
      assert.notEqual(interrupted.executor, previousExecutor);
      b.settle(f.config.authorization.slots[waiting.round]);
      const recovered = await runStudy(f.root, {
        resume: true,
        models: modelsFor(f.root).models,
        experiments: b.experiments,
      });
      assert.equal(recovered.status, "completed", JSON.stringify(recovered));
      assert.equal(
        readRecord(f.root, "r" + String(waiting.round + 1).padStart(4, "0"))
          .conversations.execute,
        interrupted.executor,
      );
      assert.equal(b.starts.length, 2);
      assert.equal(new Set(b.starts).size, 2);
      assert.deepEqual((await studyStatus(f.root)).state?.permits, []);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "autonomous continuation consumes completed evidence while unrelated sibling remains active",
  native,
  async () => {
    const f = fixture(2, 3);
    try {
      const m = modelsFor(f.root),
        b = backend(f);
      const outcome = await runStudy(f.root, {
        models: m.models,
        experiments: b.experiments,
      });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      const third = readRecord(f.root, "r0003");
      assert.deepEqual(third.task.basedOn, ["r0002"]);
      const log = events(f.root);
      assert.ok(
        log.findIndex(
          (e) =>
            e.event === "role.active" &&
            e.conversation === third.conversations.plan,
        ) <
          log.findIndex(
            (e) => e.event === "record.published" && e.id === "r0001",
          ),
      );
    } finally {
      f.cleanup();
    }
  },
);

test(
  "explicit earlier record dependencies wait, impossible references fail configuration",
  native,
  async () => {
    const f = fixture(2);
    try {
      f.config.tasks[1].basedOn = ["r0001"];
      writeFileSync(join(f.root, "study.json"), JSON.stringify(f.config));
      const b = backend(f),
        outcome = await runStudy(f.root, {
          models: modelsFor(f.root).models,
          experiments: b.experiments,
        });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      assert.deepEqual(readRecord(f.root, "r0002").plan.basedOn, ["r0001"]);
      f.config.tasks[0].basedOn = ["r0001"];
      assert.throws(() => validateConfig(f.config), /earlier round/);
      f.config.tasks[0].basedOn = [];
      f.config.tasks[1].basedOn = ["r00001"];
      assert.throws(() => validateConfig(f.config), /earlier round/);
      f.config.tasks[1].basedOn = [];
      f.config.concurrency = "2" as unknown as StudyConfig["concurrency"];
      assert.throws(() => validateConfig(f.config), /must be an object/);
      f.config.concurrency = { experiments: 0 };
      assert.throws(() => validateConfig(f.config), /positive safe integers/);
      f.config.concurrency = {};
      assert.doesNotThrow(() => validateConfig(f.config));
    } finally {
      f.cleanup();
    }
  },
);

for (const reason of ["STOP", "deadline"] as const)
  test(
    `concurrent ${reason} stops admissions and retains unknown physical capacity`,
    native,
    async () => {
      const f = fixture(1, 3);
      let finished = false;
      try {
        if (reason === "deadline") {
          f.config.authorization.expiresAt = Date.now() + 1200;
          writeFileSync(join(f.root, "study.json"), JSON.stringify(f.config));
        }
        let started!: () => void;
        const began = new Promise<void>((resolve) => {
          started = resolve;
        });
        const starts: string[] = [];
        const experiments: ExperimentBackend = {
          execution: f.config.execution!,
          readReceipts: () => [],
          run: async (_root, _config, _workspace, slot) => {
            starts.push(slot);
            started();
            await delay(1500);
            finished = true;
            throw new Error("Original synthetic job remains unknown");
          },
        };
        const running = runStudy(f.root, {
          models: modelsFor(f.root, { overlap: true }).models,
          experiments,
        });
        await began;
        if (reason === "STOP")
          writeFileSync(join(f.root, "STOP"), "operator stop\n");
        const outcome = await running;
        assert.ok(
          outcome.status === "aborted" || outcome.status === "faulted",
          JSON.stringify(outcome),
        );
        if (outcome.status === "faulted")
          assert.match(outcome.error.message, /STOP|deadline/);
        const state = (await studyStatus(f.root)).state!;
        assert.equal(starts.length, 1);
        assert.deepEqual(state.permits, starts);
        assert.equal(state.records.length, 0);
        const end = Date.now() + 3000;
        while (!finished) {
          assert.ok(Date.now() < end);
          await delay(10);
        }
      } finally {
        f.cleanup();
      }
    },
  );

test(
  "an achieved Review latches admission stop while an existing sibling drains",
  native,
  async () => {
    const f = fixture(2, 3);
    try {
      f.config.minimumRounds = 1;
      writeFileSync(join(f.root, "study.json"), JSON.stringify(f.config));
      const b = backend(f),
        outcome = await runStudy(f.root, {
          models: modelsFor(f.root, { goal: true }).models,
          experiments: b.experiments,
        });
      assert.equal(outcome.status, "completed", JSON.stringify(outcome));
      const state = (await studyStatus(f.root)).state!;
      assert.equal(state.records.length, 2);
      assert.equal(state.investigations.length, 2);
      assert.deepEqual(state.stopped, {
        goal: "achieved",
        reason: "reviewed goal achieved",
      });
      assert.equal(readRecord(f.root, "r0001").review.goal, "not-achieved");
    } finally {
      f.cleanup();
    }
  },
);

test(
  "old serial execution state is rejected before writable recovery and remains inspectable",
  native,
  async () => {
    const f = fixture();
    try {
      const opened = await openStudy(f.root, {
        models: modelsFor(f.root).models,
        experiments: backend(f).experiments,
      });
      try {
        const conversation = await opened.harness.root(BACKGROUND_CONTEXT);
        await conversation.commit(async (tx) => {
          const state = await tx.doc(opened.State);
          state.task = 999;
          const legacy = state as unknown as Record<string, unknown>;
          delete legacy.investigations;
          delete legacy.permits;
          delete legacy.autonomousEvidence;
        }, BACKGROUND_CONTEXT);
      } finally {
        await opened.close();
      }
      const database = readFileSync(join(f.root, "runtime.sqlite"));
      const m = modelsFor(f.root);
      await assert.rejects(
        runStudy(f.root, {
          models: m.models,
          experiments: backend(f).experiments,
        }),
        /Unsupported serial Study execution state/,
      );
      assert.equal(m.provider.state.callCount, 0);
      assert.deepEqual(readFileSync(join(f.root, "runtime.sqlite")), database);
      assert.deepEqual((await studyStatus(f.root)).state?.records, []);
    } finally {
      f.cleanup();
    }
  },
);
