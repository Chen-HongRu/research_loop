import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { createModels } from "@earendil-works/pi-ai/models";
import {
  Harness,
  ToolTask,
  createRegistry,
  defineDoc,
  defineExtension,
  defineTask,
  hook,
  type ConversationId,
  type SubmissionId,
  type TaskId,
} from "@earendil-works/pi-durable";
import { NodeExecutionEnv } from "@earendil-works/pi-durable/env/node";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { existsSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import {
  PROMPT,
  checkDeadline,
  initRun,
  readRun,
  trace,
  verify,
} from "./common.ts";
import { createRuntime } from "./provider.ts";

const context = BACKGROUND_CONTEXT;
type Result = ReturnType<typeof verify> & { answer: string };
type Checkpoint =
  | { phase: "start" }
  | { phase: "coding"; conversation: ConversationId }
  | {
      phase: "verify";
      conversation: ConversationId;
      submission: SubmissionId;
      answer: string;
    };
// Only an identity pointer; the framework task/checkpoints are the execution authority.
const Episode = defineDoc<{ task: number | null }>({
  kind: "spike.episode",
  version: 1,
  scope: "session",
  initial: () => ({ task: null }),
  checkpointWhen: () => false,
});

const Coding = defineTask<{ root: string }, Checkpoint, Result>({
  name: "spike.coding",
  version: 1,
  initial: () => ({ phase: "start" }),
  phases: {
    start: async (task, runtime, ctx) => {
      checkDeadline(readRun(task.input.root));
      await runtime.commit(async (tx) => {
        const child = await tx.createConversation({
          ownership: { kind: "task", taskId: task.id },
        });
        return {
          status: "running",
          checkpoint: { phase: "coding", conversation: child.id },
        };
      }, ctx);
    },
    coding: async (task, runtime, ctx) => {
      const config = readRun(task.input.root);
      checkDeadline(config);
      const child = (await runtime.conversation(
        task.state.checkpoint.conversation,
        ctx,
      ))!;
      const submission = await child.submit(
        { type: "input", content: PROMPT, requestId: config.id },
        ctx,
      );
      trace(task.input.root, "durable.submission", {
        conversation: child.id,
        submission: submission.id,
      });
      const settled = await submission.wait(ctx);
      await child.waitForIdle(ctx);
      if (settled.status !== "done")
        throw new Error(
          `Coding stopped without an answer: ${JSON.stringify(settled)}`,
        );
      const messages = (await runtime.context(child.id, ctx)).messages;
      const answer =
        messages
          .filter((message) => message.role === "assistant")
          .at(-1)
          ?.content.filter((block) => block.type === "text")
          .map((block) => block.text)
          .join("") ?? "";
      trace(task.input.root, "durable.answer.persisted", {
        submission: submission.id,
      });
      if (process.env.RL_SPIKE_FAULT === "ack")
        process.kill(process.pid, "SIGKILL");
      await runtime.commit(
        () => ({
          status: "running",
          checkpoint: {
            phase: "verify",
            conversation: child.id,
            submission: submission.id,
            answer,
          },
        }),
        ctx,
      );
    },
    verify: async (task, runtime, ctx) => {
      const config = readRun(task.input.root);
      checkDeadline(config);
      const answer = task.state.checkpoint.answer;
      if (!answer.trim()) throw new Error("Coding ended without an answer");
      const result = { ...verify(config), answer };
      trace(task.input.root, "verification", result);
      if (!result.passed)
        throw new Error(
          "Independent workspace tests failed; the model's success claim was not accepted.",
        );
      await runtime.commit(
        () => ({
          status: "terminal",
          outcome: { status: "completed", result },
        }),
        ctx,
      );
    },
  },
  abort: (_task, runtime, ctx) =>
    runtime.commit(
      () => ({ status: "terminal", outcome: { status: "aborted" } }),
      ctx,
    ),
});

async function open(root: string, readOnly = false) {
  const config = readRun(root);
  if (config.candidate !== "durable")
    throw new Error("This is not a Pi Durable run");
  if (readOnly && !existsSync(join(root, "durable.sqlite")))
    throw new Error("No durable session yet");
  const registry = createRegistry();
  registry.install(CodingTools);
  registry.install(
    defineExtension({
      name: "spike",
      tasks: [Coding],
      hooks: [
        hook(ToolTask, {
          beforeTool(call) {
            checkDeadline(config);
            trace(root, "tool.start", {
              name: call.name,
              arguments: call.arguments,
            });
            return undefined;
          },
          afterTool(call, result) {
            trace(root, "tool.end", { name: call.name, result });
            if (process.env.RL_SPIKE_FAULT === "edit" && call.name === "edit")
              process.kill(process.pid, "SIGKILL");
            return undefined;
          },
        }),
      ],
    }),
  );
  return Harness.open(
    await openNodeSqliteStorage(join(root, "durable.sqlite")),
    {
      readOnly,
      registry,
      models: readOnly ? createModels() : await createRuntime(root, config),
      env: () => new NodeExecutionEnv({ cwd: config.workspace }),
      settings: {
        retry: { maxRetries: 1 },
        compaction: { enabled: false },
        toolExecution: "sequential",
      },
      onReport: (report) =>
        trace(root, "durable.report", {
          kind: report.kind,
          error: String(report.error),
        }),
    },
    context,
  );
}

export async function runDurable(root: string) {
  root = resolve(root);
  const config = readRun(root);
  const harness = await open(root);
  let timer: ReturnType<typeof setInterval> | undefined;
  let stopping = false;
  const interrupt = () =>
    writeFileSync(join(root, "STOP"), "user cancellation\n");
  process.on("SIGINT", interrupt);
  const close = () => {
    void harness.close(context);
  };
  process.on("SIGTERM", close);
  try {
    const conversation = await harness.root(context, {
      agent: {
        model: { provider: config.provider, modelId: config.model },
        thinkingLevel: config.reasoning,
        cwd: config.workspace,
        instructions:
          "You are a coding agent working in a disposable fixture workspace.",
      },
    });
    const id = await conversation.commit(async (tx) => {
      const ref = await tx.doc(Episode);
      if (ref.task === null) {
        checkDeadline(config);
        ref.task = await tx.createTask(
          Coding,
          { root },
          { ownership: { kind: "conversation" } },
        );
      }
      return ref.task as TaskId<Result>;
    }, context);
    const stop = async () => {
      if (
        !stopping &&
        (Date.now() >= config.deadline || existsSync(join(root, "STOP")))
      ) {
        stopping = true;
        trace(root, "cancellation", {
          deadline: config.deadline,
          stop: existsSync(join(root, "STOP")),
        });
        await harness.abortTask(id, context);
      }
    };
    await stop();
    timer = setInterval(() => {
      void stop();
    }, 50);
    const result = await harness.waitForTask(id, context);
    trace(root, "durable.outcome", { state: result.state });
    return result.state.outcome;
  } finally {
    if (timer) clearInterval(timer);
    process.off("SIGINT", interrupt);
    process.off("SIGTERM", close);
    await harness.close(context);
  }
}

export async function statusDurable(root: string) {
  const harness = await open(resolve(root), true);
  try {
    const ref = await harness.snapshot(Episode, context);
    return {
      task: ref?.task
        ? await harness.getTask(ref.task as TaskId<Result>, context)
        : null,
      live: await harness.inspect(context),
      usage: await harness.usage(context),
    };
  } finally {
    await harness.close(context);
  }
}

export function cancelDurable(root: string) {
  readRun(root);
  writeFileSync(join(root, "STOP"), "user cancellation\n");
  return {
    stopRequested: true,
    note: "The running host observes STOP and aborts its owned task. If stopped, run again to commit the abort; do not delete STOP.",
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const [command, root, provider, model, reasoning] = process.argv.slice(2);
  try {
    if (!root)
      throw new Error(
        "Usage: node durable.ts init|run|status|cancel RUN_DIRECTORY [PROVIDER MODEL [off|medium]]",
      );
    const result =
      command === "init"
        ? initRun(root, "durable", {
            ...(provider ? { provider, model } : {}),
            reasoning: reasoning as "off" | "medium" | undefined,
          })
        : command === "run"
          ? await runDurable(root)
          : command === "status"
            ? await statusDurable(root)
            : command === "cancel"
              ? cancelDurable(root)
              : (() => {
                  throw new Error("Unknown command");
                })();
    console.log(JSON.stringify(result, null, 2));
    if (
      command === "run" &&
      (result as { status?: string }).status !== "completed"
    )
      process.exitCode = 1;
  } catch (error) {
    console.error(String(error));
    process.exitCode = 1;
  }
}
