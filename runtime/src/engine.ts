import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import type { JsonValue } from "@earendil-works/chord";
import { createModels, type Models } from "@earendil-works/pi-ai/models";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import {
  Harness,
  ToolTask,
  configure,
  createRegistry,
  defineDoc,
  defineExtension,
  defineTask,
  defineTool,
  hook,
  type ConversationId,
  type TaskId,
  type Task,
  type TaskRuntime,
} from "@earendil-works/pi-durable";
import { openNodeSqliteStorage } from "@earendil-works/pi-durable/storage/sqlite/node";
import { CodingTools } from "@earendil-works/pi-durable/tools";
import { getOrThrow } from "@earendil-works/pi-durable/env";
import { obj } from "@earendil-works/pi-schema";
import {
  assembleContext,
  contextAccess,
  executeSchema,
  notesSchema,
  planSchema,
  previewContext,
  previewDefinitions,
  reviewSchema,
  type RoleContext,
  type RoleRequest,
} from "./context.ts";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import type {
  Plan,
  ExperimentBackend,
  Receipt,
  ResearchRecord,
  ResearchTask,
  Review,
  StudyConfig,
} from "./contract.ts";
import {
  admission,
  event,
  publishKnowledge,
  publishRecord,
  readConfig,
  readRecord,
  readRegular,
  writeJson,
} from "./files.ts";
import { createEnvironment, requireNativePlatform } from "./environment.ts";
import { readReceipts, runExperiment } from "./experiments.ts";

const context = BACKGROUND_CONTEXT;
const Binding = defineDoc<{
  role: string;
  workspace: string;
  assembled: RoleContext | null;
  records: string[];
  slot: string;
}>({
  kind: "research.role",
  version: 1,
  scope: "conversation",
  history: "latest",
  fork: "initial",
  initial: () => ({
    role: "",
    workspace: "",
    assembled: null,
    records: [],
    slot: "",
  }),
  checkpointWhen: () => false,
});
const Answer = defineDoc<{ value: JsonValue | null }>({
  kind: "research.answer",
  version: 1,
  scope: "conversation",
  history: "latest",
  fork: "initial",
  initial: () => ({ value: null }),
  checkpointWhen: () => false,
});
const State = defineDoc<{
  task: number | null;
  records: string[];
  pending: string[];
  manager: number | null;
  investigations: {
    round: number;
    task: number;
    planner?: number;
    executor?: number;
    plan?: {
      question: string;
      change: string;
      rationale: string;
      basedOn: string[];
    };
  }[];
  permits: string[];
  autonomousEvidence: string[];
  stopped: { goal: string; reason: string } | null;
  publications: { id: string; records: string[]; at: string }[];
}>({
  kind: "research.study",
  version: 1,
  scope: "session",
  initial: () => ({
    task: null,
    records: [],
    pending: [],
    manager: null,
    investigations: [],
    permits: [],
    autonomousEvidence: [],
    stopped: null,
    publications: [],
  }),
  checkpointWhen: () => false,
});

type Input = { root: string; config: StudyConfig; recovery?: number[] };
type InvestigationInput = Input & {
  round: number;
  task: ResearchTask;
  evidence: string[];
  resume?: RoundState;
};
type StudyState =
  | { phase: "admit"; next: number; active: number[] }
  | { phase: "join"; next: number; active: number[] };
type SavedExperiment = {
  round: number;
  plan: Plan;
  task: ResearchTask;
  planner: ConversationId;
  executor: ConversationId;
  workspace: string;
};
type RoundState =
  | { phase: "reconciled"; experiment: SavedExperiment }
  | { phase: "plan"; round: number }
  | {
      phase: "awaitPlan";
      round: number;
      conversation: ConversationId;
      task: ResearchTask;
    }
  | {
      phase: "execute";
      round: number;
      plan: Plan;
      task: ResearchTask;
      planner: ConversationId;
    }
  | {
      phase: "awaitExecute";
      round: number;
      plan: Plan;
      task: ResearchTask;
      planner: ConversationId;
      executor: ConversationId;
      workspace: string;
    }
  | {
      phase: "awaitReview";
      round: number;
      plan: Plan;
      task: ResearchTask;
      planner: ConversationId;
      executor: ConversationId;
      reviewer: ConversationId;
      execute: string;
      receipts: Receipt[];
    }
  | { phase: "record"; round: number; record: ResearchRecord };
type LoopResult = { records: string[]; goal: string; stopped: string };
type ManagerState =
  | { phase: "prepare" }
  | { phase: "edit"; conversation: ConversationId };

function recordId(round: number) {
  return "r" + String(round + 1).padStart(4, "0");
}
function previous(root: string, ids: string[]) {
  return ids.map((id) => readRecord(root, id));
}

async function ask<I, S, R>(
  root: string,
  runtime: TaskRuntime<I, S, R, object>,
  id: ConversationId,
  requestId: string,
) {
  const binding = await runtime.snapshot(Binding, id, context);
  if (!binding?.assembled)
    throw new Error(
      "Role has no frozen context; inspect and reconcile using its original runtime",
    );
  event(root, "role.active", { role: binding?.role, conversation: id });
  const conversation = (await runtime.conversation(id, context))!;
  const submission = await conversation.submit(
    { type: "input", requestId, content: binding.assembled.prompt },
    context,
  );
  const settled = await submission.wait(context);
  await conversation.waitForIdle(context);
  if (settled.status !== "done")
    throw new Error("Role ended without a committed result");
  const answer = await runtime.snapshot(Answer, id, context);
  if (answer?.value === null || answer === undefined)
    throw new Error("Role must use its structured finish tool");
  event(root, "role.finished", { role: binding?.role, conversation: id });
  return answer.value;
}

export async function openStudy(
  root: string,
  options: {
    readOnly?: boolean;
    models?: Models;
    experiments?: ExperimentBackend;
  } = {},
): Promise<{
  harness: Harness;
  config: StudyConfig;
  Loop: Task<Input, StudyState, LoopResult, object>;
  Manager: Task<Input & { records: string[] }, ManagerState, null, object>;
  State: typeof State;
  experiments: Pick<ExperimentBackend, "run" | "readReceipts">;
  close(): Promise<void>;
}> {
  root = resolve(root);
  const config = readConfig(root);
  const readOnly = options.readOnly ?? false;
  if (!readOnly && existsSync(join(root, "runtime.sqlite"))) {
    // Inspect the persisted contract before a writable Harness can recover task state.
    const inspection = await openStudy(root, { readOnly: true });
    try {
      const state = await inspection.harness.snapshot(State, context);
      if (
        state?.task &&
        (!Array.isArray(state.investigations) ||
          !Array.isArray(state.permits) ||
          !Array.isArray(state.autonomousEvidence))
      )
        throw new Error(
          "Unsupported serial Study execution state; inspect it read-only and reconcile original effects before creating a new Study",
        );
      const original = state?.task
        ? await inspection.harness.getTask(state.task as TaskId, context)
        : null;
      if (
        original &&
        JSON.stringify((original.input as unknown as Input).config) !==
          JSON.stringify(config)
      )
        throw new Error(
          "Original Study contract changed; authorization cannot be refreshed by editing configuration",
        );
    } finally {
      await inspection.close();
    }
  }
  if (config.execution && !options.experiments && !readOnly)
    throw new Error(
      "This Study requires its exact configured experiment adapter",
    );
  if (
    options.experiments &&
    JSON.stringify(config.execution) !==
      JSON.stringify(options.experiments.execution)
  )
    throw new Error(
      "Execution adapter does not match the frozen Study contract",
    );
  const experiments = options.experiments ?? {
    run: runExperiment,
    readReceipts,
  };
  if (!readOnly) requireNativePlatform();
  if (readOnly && !existsSync(join(root, "runtime.sqlite")))
    throw new Error("Study has not started");
  const environments = new Map<
    number,
    Awaited<ReturnType<typeof createEnvironment>>
  >();
  const configuredModels =
    options.models ??
    (readOnly
      ? createModels()
      : await ModelRuntime.create({ allowModelNetwork: false }));
  if (
    !readOnly &&
    !configuredModels.getModel(config.model.provider, config.model.id)
  )
    throw new Error("Exact configured provider/model unavailable; no fallback");
  const stream: Models["streamSimple"] = (model, messages, settings) => {
    admission(root, config);
    event(root, "model.request", { model: model.id });
    return configuredModels.streamSimple(model, messages, settings);
  };
  const models = readOnly
    ? configuredModels
    : new Proxy(configuredModels, {
        get(target, key) {
          if (key === "streamSimple") return stream;
          const value = Reflect.get(target, key, target);
          return typeof value === "function" ? value.bind(target) : value;
        },
      });

  const finishPlan = defineTool({
    name: "submit_plan",
    description:
      "Freeze your research question, concrete code change, rationale and reviewed record IDs. This ends planning.",
    parameters: planSchema,
    replay: "safe",
    async execute(args, api, ctx) {
      const role = await api.snapshot(Binding, api.conversationId, ctx);
      if (role?.role !== "plan")
        throw new Error("Only the Planner can submit a Plan");
      if (
        role.records.length &&
        !args.basedOn.some((id) => role.records.includes(id))
      )
        throw new Error("Use a specific prior reviewed record ID in basedOn");
      if (args.basedOn.some((id) => !role.records.includes(id)))
        throw new Error("Unknown reviewed record reference");
      await api.commit(async (tx) => {
        const answer = await tx.doc(Answer, api.conversationId);
        if (answer.value === null) {
          answer.value = args;
          const state = await tx.doc(State);
          const investigation = state.investigations.find(
            (row) => row.planner === api.conversationId,
          );
          if (!investigation)
            throw new Error("Planner has no assigned investigation");
          investigation.plan = args;
        }
      }, ctx);
      if (process.env.RESEARCH_FAULT === "plan-ack")
        process.kill(process.pid, "SIGKILL");
      return {
        output: [{ type: "text", text: "Plan frozen" }],
        control: { terminate: true },
      };
    },
  });
  const finishExecute = defineTool({
    name: "finish_execute",
    description:
      "Finish coding with a public summary of actual changes and limitations. Measurements come only from run_experiment receipts.",
    parameters: executeSchema,
    replay: "safe",
    async execute(args, api, ctx) {
      if (
        (await api.snapshot(Binding, api.conversationId, ctx))?.role !==
        "execute"
      )
        throw new Error("Not Execute");
      await api.commit(async (tx) => {
        const answer = await tx.doc(Answer, api.conversationId);
        if (answer.value === null) answer.value = args.summary;
      }, ctx);
      return {
        output: [{ type: "text", text: "Execute finished" }],
        control: { terminate: true },
      };
    },
  });
  const finishReview = defineTool({
    name: "submit_review",
    description:
      "Independently interpret actual Plan, candidate and trusted raw receipts. Separate validity, knowledge change, goal and continuation; lack of evidence is not success.",
    parameters: reviewSchema,
    replay: "safe",
    async execute(args, api, ctx) {
      if (
        (await api.snapshot(Binding, api.conversationId, ctx))?.role !==
        "review"
      )
        throw new Error("Not independent Review");
      const completed = (await api.snapshot(State, ctx))?.records.length ?? 0;
      if (
        args.goal === "achieved" &&
        completed + 1 < (config.minimumRounds ?? 1)
      )
        throw new Error(
          "The human goal requires more reviewed investigations; do not declare it achieved early",
        );
      await api.commit(async (tx) => {
        const answer = await tx.doc(Answer, api.conversationId);
        if (answer.value === null) answer.value = args;
      }, ctx);
      return {
        output: [{ type: "text", text: "Review committed" }],
        control: { terminate: true },
      };
    },
  });
  const experiment = defineTool({
    name: "run_experiment",
    description:
      "Run this round's single pre-authorized experiment through its frozen backend. Freezes the current candidate and returns a trusted receipt. Repeating cannot create another job. Unknown submission requires operator reconciliation.",
    parameters: obj({}),
    async execute(_args, api, ctx) {
      const role = await api.snapshot(Binding, api.conversationId, ctx);
      if (role?.role !== "execute")
        throw new Error("Only Execute may submit experiments");
      admission(root, config);
      // Capacity is reserved durably before entering a backend with external effects.
      // Only a canonical collected receipt can free it, including terminal failures.
      while (true) {
        const watch = (await api.watchDoc(State, ctx))!;
        try {
          const acquired = await api.commit(async (tx) => {
            admission(root, config);
            const state = await tx.doc(State);
            const receipts = experiments.readReceipts(root, config);
            state.permits = state.permits.filter(
              (slot) => !receipts.some((r) => r.slot === slot),
            );
            const existing = receipts.find((r) => r.slot === role.slot);
            if (existing) return "collected";
            if (state.permits.includes(role.slot)) return "unknown";
            if (state.permits.length >= (config.concurrency?.experiments ?? 1))
              return "wait";
            state.permits.push(role.slot);
            return "acquired";
          }, ctx);
          if (acquired === "unknown")
            throw new Error(
              "Original experiment effect is pending or unknown; query/bind/collect it before resume, never resubmit",
            );
          if (acquired !== "wait") break;
          await new Promise<void>((resolve, reject) => {
            watch.start(async (call) => {
              if (call.kind === "failed") reject(call.error);
              else if (
                call.value.permits.length <
                (config.concurrency?.experiments ?? 1)
              )
                resolve();
            });
            void watch.closed.then(() =>
              reject(new Error("Experiment capacity wait ended")),
            );
          });
        } finally {
          await watch.stop();
        }
      }
      admission(root, config);
      let receipt: Receipt;
      try {
        receipt =
          experiments
            .readReceipts(root, config)
            .find((r) => r.slot === role.slot) ??
          (await experiments.run(root, config, role.workspace, role.slot));
      } finally {
        // Do not release on tool error, controller death, or unknown physical state.
        const receipts = experiments.readReceipts(root, config);
        if (receipts.some((r) => r.slot === role.slot))
          await api.commit(async (tx) => {
            const state = await tx.doc(State);
            state.permits = state.permits.filter((slot) => slot !== role.slot);
          }, ctx);
      }
      return { output: [{ type: "text", text: JSON.stringify(receipt) }] };
    },
  });
  const notes = defineTool({
    name: "publish_notes",
    description:
      "Finish editing your research.md draft for atomic publication when this Manager conversation succeeds. Reviewed records remain authoritative; do not invent measurements.",
    parameters: notesSchema,
    replay: "safe",
    async execute(_args, api, ctx) {
      const role = await api.snapshot(Binding, api.conversationId, ctx);
      if (role?.role !== "manager")
        throw new Error("Only the serial Manager can publish notes");
      const frozen = await api.snapshot(Answer, api.conversationId, ctx);
      if (frozen === undefined || frozen.value === null) {
        const reader = getOrThrow(
          await api.env!.openBinaryReader(
            "research.md",
            { noFollow: true },
            ctx,
          ),
        );
        let content: string;
        try {
          const info = getOrThrow(await reader.info(ctx));
          if (info.kind !== "file" || info.size > 1_048_576)
            throw new Error(
              "Knowledge draft must be a regular file of at most 1 MiB",
            );
          const bytes = getOrThrow(await reader.read(0, 1_048_577, ctx));
          if (bytes.length > 1_048_576)
            throw new Error("Knowledge draft exceeds 1 MiB");
          content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
        } finally {
          await reader.close(ctx);
        }
        await api.commit(async (tx) => {
          const answer = await tx.doc(Answer, api.conversationId);
          if (answer.value === null)
            answer.value = { content, preparedAt: new Date().toISOString() };
        }, ctx);
      }
      return {
        output: [
          {
            type: "text",
            text: "Draft frozen for publication after successful completion",
          },
        ],
        control: { terminate: true },
      };
    },
  });

  const allTools = [
    ...CodingTools.tools!,
    finishPlan,
    finishExecute,
    finishReview,
    experiment,
    notes,
  ];
  async function roleInCommit(
    tx: Parameters<Parameters<Harness["commit"]>[0]>[0],
    owner: TaskId,
    request: RoleRequest,
    slot = "",
  ) {
    const assembled = assembleContext(root, config, request);
    const modelTools = assembled.tools.map((name) => {
      const tool = allTools.find((tool) => tool.name === name);
      if (!tool) throw new Error(`Unregistered context tool: ${name}`);
      return tool;
    });
    const conversation = await tx.createConversation({
      ownership: { kind: "task", taskId: owner },
    });
    Object.assign(await tx.doc(Binding, conversation.id), {
      role: assembled.role,
      workspace: assembled.workspace,
      records: request.records.map((record) => record.id),
      slot,
      assembled,
    });
    await configure(tx, conversation.id, {
      cwd: assembled.workspace,
      model: { provider: config.model.provider, modelId: config.model.id },
      thinkingLevel: config.model.thinking,
      modelTools,
      instructions: assembled.instructions,
    });
    return conversation.id;
  }

  const Manager = defineTask<Input & { records: string[] }, ManagerState, null>(
    {
      name: "research.manager",
      version: 1,
      initial: () => ({ phase: "prepare" }),
      phases: {
        prepare: async (task, runtime, ctx) => {
          admission(root, config);
          const workspace = join(root, "manager", String(task.id));
          mkdirSync(workspace, { recursive: true });
          writeFileSync(
            join(workspace, "research.md"),
            readFileSync(join(root, "knowledge", "research.md")),
          );
          await runtime.commit(
            async (tx) => ({
              status: "running",
              checkpoint: {
                phase: "edit",
                conversation: await roleInCommit(tx, task.id, {
                  role: "manager",
                  workspace,
                  records: previous(root, task.input.records),
                }),
              },
            }),
            ctx,
          );
        },
        edit: async (task, runtime, ctx) => {
          const publication = (await ask(
            root,
            runtime,
            task.state.checkpoint.conversation,
            "manager",
          )) as { content: string; preparedAt: string };
          const id = String(task.id);
          const replaced = publishKnowledge(root, id, publication.content);
          if (process.env.RESEARCH_FAULT === "manager-ack")
            process.kill(process.pid, "SIGKILL");
          event(
            root,
            replaced ? "knowledge.published" : "knowledge.recovered",
            {
              id,
              records: task.input.records,
            },
          );
          await runtime.commit(async (tx) => {
            const state = await tx.doc(State);
            state.pending = state.pending.filter(
              (record) => !task.input.records.includes(record),
            );
            if (!state.publications.some((row) => row.id === id))
              state.publications.push({
                id,
                records: task.input.records,
                at: publication.preparedAt,
              });
            state.manager = null;
            if (state.pending.length)
              state.manager = await tx.createTask(
                Manager,
                { root, config, records: [...state.pending] },
                { ownership: { kind: "conversation" }, background: true },
              );
            return {
              status: "terminal",
              outcome: { status: "completed", result: null },
            };
          }, ctx);
        },
      },
      abort: (_task, runtime, ctx) =>
        runtime.commit(
          () => ({ status: "terminal", outcome: { status: "aborted" } }),
          ctx,
        ),
    },
  );

  const Investigation = defineTask<
    InvestigationInput,
    RoundState,
    ResearchRecord
  >({
    name: "research.investigation",
    version: 1,
    initial: (input) => input.resume ?? { phase: "plan", round: input.round },
    phases: {
      reconciled: async (task, runtime, ctx) => {
        admission(root, config);
        const cp = task.state.checkpoint.experiment;
        const receipts = experiments
          .readReceipts(root, config)
          .filter(
            (receipt) => receipt.slot === config.authorization.slots[cp.round],
          );
        if (!receipts.length)
          throw new Error(
            "Bind and collect the original job before continuing; no new submission is permitted",
          );
        const answer = await runtime.snapshot(Answer, cp.executor, ctx);
        await runtime.commit(
          async (tx) => ({
            status: "running",
            checkpoint: {
              phase: "awaitReview",
              ...cp,
              execute:
                typeof answer?.value === "string"
                  ? answer.value
                  : "Interrupted Execute; original job reconciled by operator, no coding or job replay",
              receipts,
              reviewer: await roleInCommit(tx, task.id, {
                role: "review",
                workspace: cp.workspace,
                records: previous(root, cp.plan.basedOn),
                round: cp.round,
                task: cp.task,
                plan: cp.plan,
                execute:
                  typeof answer?.value === "string"
                    ? answer.value
                    : "Interrupted Execute; original job reconciled by operator, no coding or job replay",
                receipts,
              }),
            },
          }),
          ctx,
        );
      },
      plan: async (task, runtime, ctx) => {
        admission(root, config);
        const selected = task.input.task;
        const workspace = join(
          root,
          "workspaces",
          recordId(task.state.checkpoint.round),
        );
        mkdirSync(workspace, { recursive: true });
        const base = selected.basedOn.at(-1);
        const preceding = base ? readRecord(root, base) : null;
        const source =
          preceding?.receipts.find((receipt) => receipt.valid)?.candidatePath ??
          config.source.root;
        for (const file of config.source.files) {
          mkdirSync(dirname(join(workspace, file)), { recursive: true });
          writeFileSync(join(workspace, file), readRegular(join(source, file)));
        }
        await runtime.commit(async (tx) => {
          const conversation = await roleInCommit(tx, task.id, {
            role: "plan",
            workspace,
            records: previous(root, task.input.evidence),
            task: selected,
          });
          const state = await tx.doc(State);
          state.investigations.find((row) => row.task === task.id)!.planner =
            conversation;
          return {
            status: "running",
            checkpoint: {
              phase: "awaitPlan",
              round: task.state.checkpoint.round,
              task: selected,
              conversation,
            },
          };
        }, ctx);
      },
      awaitPlan: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        const plan = (await ask(
          root,
          runtime,
          cp.conversation,
          "plan",
        )) as unknown as Plan;
        const selected =
          cp.task.source === "human"
            ? cp.task
            : { ...cp.task, question: plan.question, basedOn: plan.basedOn };
        await runtime.commit(
          () => ({
            status: "running",
            checkpoint: {
              phase: "execute",
              round: cp.round,
              plan,
              task: selected,
              planner: cp.conversation,
            },
          }),
          ctx,
        );
      },
      execute: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        const workspace = join(root, "workspaces", recordId(cp.round));
        await runtime.commit(async (tx) => {
          const executor = await roleInCommit(
            tx,
            task.id,
            {
              role: "execute",
              workspace,
              records: previous(root, task.input.evidence),
              task: cp.task,
              plan: cp.plan,
            },
            config.authorization.slots[cp.round],
          );
          const state = await tx.doc(State);
          state.investigations.find((row) => row.task === task.id)!.executor =
            executor;
          return {
            status: "running",
            checkpoint: {
              phase: "awaitExecute",
              round: cp.round,
              plan: cp.plan,
              task: cp.task,
              planner: cp.planner,
              workspace,
              executor,
            },
          };
        }, ctx);
      },
      awaitExecute: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        const accepted: SavedExperiment = {
          round: cp.round,
          plan: cp.plan,
          task: cp.task,
          planner: cp.planner,
          executor: cp.executor,
          workspace: cp.workspace,
        };
        const path = join(root, "rounds", recordId(cp.round) + ".json");
        if (existsSync(path)) {
          const saved = JSON.parse(
            readFileSync(path, "utf8"),
          ) as SavedExperiment;
          if (
            JSON.stringify({ ...saved, executor: accepted.executor }) !==
            JSON.stringify(accepted)
          )
            throw new Error("Accepted experiment identity changed");
        } else writeJson(path, accepted);
        const summary = await ask(root, runtime, cp.executor, "execute");
        const receipts = experiments
          .readReceipts(root, config)
          .filter(
            (receipt) => receipt.slot === config.authorization.slots[cp.round],
          );
        if (receipts.length === 0)
          throw new Error(
            "Execute produced no trusted experiment receipt; self-reports are not measurements",
          );
        await runtime.commit(
          async (tx) => ({
            status: "running",
            checkpoint: {
              phase: "awaitReview",
              round: cp.round,
              plan: cp.plan,
              task: cp.task,
              planner: cp.planner,
              executor: cp.executor,
              execute: String(summary),
              receipts,
              reviewer: await roleInCommit(tx, task.id, {
                role: "review",
                workspace: cp.workspace,
                records: previous(root, cp.plan.basedOn),
                round: cp.round,
                task: cp.task,
                plan: cp.plan,
                execute: String(summary),
                receipts,
              }),
            },
          }),
          ctx,
        );
      },
      awaitReview: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        const review = (await ask(
          root,
          runtime,
          cp.reviewer,
          "review",
        )) as unknown as Review;
        if (
          !cp.receipts.some((receipt) => receipt.valid) &&
          review.goal === "achieved"
        )
          throw new Error("Invalid evidence cannot prove the goal achieved");
        const record: ResearchRecord = {
          id: recordId(cp.round),
          task: cp.task,
          plan: cp.plan,
          execute: cp.execute,
          receipts: cp.receipts,
          review,
          conversations: {
            plan: cp.planner,
            execute: cp.executor,
            review: cp.reviewer,
          },
          createdAt: new Date().toISOString(),
        };
        await runtime.commit(
          () => ({
            status: "running",
            checkpoint: { phase: "record", round: cp.round, record },
          }),
          ctx,
        );
      },
      record: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        publishRecord(root, cp.record);
        await runtime.commit(async (tx) => {
          const state = await tx.doc(State);
          if (!state.records.includes(cp.record.id)) {
            state.records.push(cp.record.id);
            state.pending.push(cp.record.id);
          }
          const existing =
            state.manager === null
              ? null
              : await tx.task(state.manager as TaskId);
          if (!existing || existing.state.status === "terminal")
            state.manager = await tx.createTask(
              Manager,
              { root, config, records: [...state.pending] },
              { ownership: { kind: "conversation" }, background: true },
            );
          if (
            !state.stopped &&
            (cp.record.review.goal === "achieved" || !cp.record.review.continue)
          )
            state.stopped = {
              goal: cp.record.review.goal,
              reason:
                cp.record.review.goal === "achieved"
                  ? "reviewed goal achieved"
                  : "reviewed stop",
            };
          return {
            status: "terminal",
            outcome: { status: "completed", result: cp.record },
          };
        }, ctx);
        event(root, "record.published", { id: cp.record.id });
      },
    },
    abort: (_task, runtime, ctx) =>
      runtime.commit(
        () => ({ status: "terminal", outcome: { status: "aborted" } }),
        ctx,
      ),
  });

  const Loop = defineTask<Input, StudyState, LoopResult>({
    name: "research.study.coordinator",
    version: 1,
    initial: () => ({ phase: "admit", next: 0, active: [] }),
    phases: {
      admit: async (task, runtime, ctx) => {
        admission(root, config);
        await runtime.commit(async (tx) => {
          const state = await tx.doc(State);
          const receipts = experiments.readReceipts(root, config);
          state.permits = state.permits.filter(
            (slot) => !receipts.some((receipt) => receipt.slot === slot),
          );
          // SDK transactions require table reads before the first task creation.
          const existingTasks = new Map<
            number,
            Awaited<ReturnType<typeof tx.task>>
          >();
          for (const row of state.investigations)
            existingTasks.set(row.task, await tx.task(row.task as TaskId));
          // Recover by assigned round, never by completion count or order.
          const active: number[] = [];
          for (const row of state.investigations) {
            const existing = existingTasks.get(row.task)!;
            if (state.records.includes(recordId(row.round))) continue;
            if (existing?.state.status !== "terminal") {
              active.push(row.task);
              continue;
            }
            if (!task.input.recovery?.includes(row.task))
              throw new Error(
                `Investigation failed; reconcile original effects and resume explicitly: ${JSON.stringify(existing.state.outcome)}`,
              );
            const saved = join(root, "rounds", recordId(row.round) + ".json");
            const input = existing.input as unknown as InvestigationInput;
            const resume: RoundState = existsSync(
              join(root, "records", recordId(row.round) + ".json"),
            )
              ? {
                  phase: "record",
                  round: row.round,
                  record: readRecord(root, recordId(row.round)),
                }
              : existsSync(saved)
                ? (() => {
                    const accepted = JSON.parse(
                      readFileSync(saved, "utf8"),
                    ) as SavedExperiment;
                    if (row.executor)
                      accepted.executor = row.executor as ConversationId;
                    if (
                      experiments
                        .readReceipts(root, config)
                        .some(
                          (r) =>
                            r.slot === config.authorization.slots[row.round],
                        )
                    )
                      return {
                        phase: "reconciled",
                        experiment: accepted,
                      } as RoundState;
                    if (
                      state.permits.includes(
                        config.authorization.slots[row.round],
                      )
                    )
                      throw new Error(
                        "Collect the bound original job before resume; no Execute replay is permitted",
                      );
                    // No durable permit means this accepted Execute never entered the backend.
                    return {
                      phase: "execute",
                      round: row.round,
                      plan: accepted.plan,
                      task: accepted.task,
                      planner: accepted.planner,
                    } as RoundState;
                  })()
                : row.plan && row.planner
                  ? {
                      phase: "execute",
                      round: row.round,
                      plan: row.plan,
                      task:
                        input.task.source === "human"
                          ? input.task
                          : {
                              ...input.task,
                              question: row.plan.question,
                              basedOn: row.plan.basedOn,
                            },
                      planner: row.planner as ConversationId,
                    }
                  : { phase: "plan", round: row.round };
            row.task = await tx.createTask(
              Investigation,
              { ...input, resume },
              { ownership: { kind: "task", taskId: task.id } },
            );
            active.push(row.task);
          }
          let next = state.investigations.length;
          const limit = config.concurrency?.investigations ?? 1;
          while (
            !state.stopped &&
            next < config.maxRounds &&
            active.length < limit
          ) {
            admission(root, config);
            const human = config.tasks[next];
            // Admit a dependent follow-up only from new, completed Reviews.
            if (
              !human &&
              next > 0 &&
              !state.records.some(
                (id) => !state.autonomousEvidence.includes(id),
              )
            )
              break;
            const selected: ResearchTask = human ?? {
              id: recordId(next),
              source: "autonomous",
              question:
                "Select a follow-up investigation from the reviewed evidence",
              constraints: [],
              basedOn: [...state.records],
            };
            if (selected.basedOn.some((id) => !state.records.includes(id)))
              break;
            // Human investigations see only explicitly selected evidence; autonomous work sees all reviewed evidence.
            const evidence = [...selected.basedOn];
            const id = await tx.createTask(
              Investigation,
              { root, config, round: next, task: selected, evidence },
              { ownership: { kind: "task", taskId: task.id } },
            );
            if (!human) state.autonomousEvidence = [...evidence];
            state.investigations.push({ round: next, task: id });
            active.push(id);
            next++;
            if (!human) break;
          }
          if (!active.length)
            return {
              status: "terminal",
              outcome: {
                status: "completed",
                result: {
                  records: [...state.records],
                  goal:
                    state.stopped?.goal ??
                    (state.records.length
                      ? readRecord(root, state.records.at(-1)!).review.goal
                      : "unknown"),
                  stopped: state.stopped?.reason ?? "authorized round limit",
                },
              },
            };
          return {
            status: "running",
            checkpoint: { phase: "join", next, active },
          };
        }, ctx);
      },
      join: async (task, runtime, ctx) => {
        // SDK owns persistence, task lifetime and restart. Wait for one completion so an
        // independent configured investigation can fill the freed investigation slot.
        await Promise.race(
          task.state.checkpoint.active.map((id) =>
            runtime.waitForTask(id as TaskId, ctx),
          ),
        );
        await runtime.commit(
          () => ({
            status: "running",
            checkpoint: {
              phase: "admit",
              next: task.state.checkpoint.next,
              active: [],
            },
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

  const registry = createRegistry();
  registry.install(CodingTools);
  registry.install(
    defineExtension({
      name: "research",
      tools: [finishPlan, finishExecute, finishReview, experiment, notes],
      tasks: [Loop, Investigation, Manager],
      hooks: [
        hook(ToolTask, {
          async beforeTool(call, api, ctx) {
            admission(root, config);
            const binding = await api.snapshot(
              Binding,
              api.conversationId,
              ctx,
            );
            if (!binding?.assembled?.tools.includes(call.name))
              throw new Error(
                `Tool ${call.name} is not granted by this conversation's frozen context`,
              );
            event(root, "tool.start", { name: call.name });
            return undefined;
          },
          afterTool(call, result) {
            event(root, "tool.end", {
              name: call.name,
              isError: result.isError ?? false,
            });
            return undefined;
          },
        }),
      ],
    }),
  );
  const harness: Harness = await Harness.open(
    await openNodeSqliteStorage(join(root, "runtime.sqlite")),
    {
      readOnly,
      models,
      registry,
      settings: {
        compaction: { enabled: false },
        retry: { maxRetries: 1 },
        toolExecution: "sequential",
      },
      env: async (target, ctx) => {
        const binding = await target.read.snapshot(
          Binding,
          target.conversationId,
          ctx,
        );
        if (!binding?.assembled)
          throw new Error(
            "Role has no frozen context; inspect and reconcile using its original runtime",
          );
        if (!environments.has(target.conversationId))
          environments.set(
            target.conversationId,
            await createEnvironment({
              id: String(target.conversationId),
              workspace: binding.assembled.workspace,
              ...contextAccess(binding.assembled),
              python: config.python,
            }),
          );
        return environments.get(target.conversationId)!.env;
      },
      onReport: (report) =>
        event(root, "framework.report", {
          kind: report.kind,
          error: String(report.error),
        }),
    },
    context,
  );
  return {
    harness,
    config,
    Loop,
    Manager,
    State,
    experiments,
    async close() {
      await harness.close(context);
      await Promise.all(
        [...environments.values()].map((value) => value.close()),
      );
    },
  };
}

function watchAuthorization(
  root: string,
  config: StudyConfig,
  conversation: Awaited<ReturnType<Harness["root"]>>,
  harness: Harness,
) {
  let stopping = false;
  const stop = async () => {
    if (stopping) return;
    if (
      existsSync(join(root, "STOP")) ||
      Date.now() >= config.authorization.expiresAt
    ) {
      stopping = true;
      event(root, "stop", {
        workflow: true,
        physicalJobCancellation: "not implied",
      });
      await conversation.abort(context, { background: true });
    }
  };
  const interrupt = () => writeFileSync(join(root, "STOP"), "operator stop\n");
  const terminate = () => {
    void harness.close(context);
  };
  process.on("SIGINT", interrupt);
  process.on("SIGTERM", terminate);
  const timer = setInterval(() => {
    void stop().catch((error) =>
      event(root, "stop.error", { error: String(error) }),
    );
  }, 50);
  return {
    stop,
    stopped: () => stopping,
    close() {
      clearInterval(timer);
      process.off("SIGINT", interrupt);
      process.off("SIGTERM", terminate);
    },
  };
}

export async function runStudy(
  root: string,
  options: {
    models?: Models;
    drainManager?: boolean;
    resume?: boolean;
    experiments?: ExperimentBackend;
  } = {},
) {
  root = resolve(root);
  const opened = await openStudy(root, options);
  const { harness, config, Loop } = opened;
  let conversation: Awaited<ReturnType<Harness["root"]>>;
  let id: TaskId<LoopResult>;
  try {
    conversation = await harness.root(context);
    if (options.resume) {
      admission(root, config);
      const state = await harness.snapshot(State, context);
      const receipts = opened.experiments.readReceipts(root, config);
      for (const row of state?.investigations ?? []) {
        if (state!.records.includes(recordId(row.round))) continue;
        if (
          state!.permits.includes(config.authorization.slots[row.round]) &&
          !receipts.some(
            (receipt) => receipt.slot === config.authorization.slots[row.round],
          )
        )
          throw new Error(
            "Collect the bound original job before resume; no Execute replay is permitted",
          );
      }
      const original = state?.task
        ? await harness.getTask(state.task as TaskId, context)
        : null;
      if (original && original.state.status !== "terminal") {
        await harness.abortTask(original.id, context);
        await harness.waitForTask(original.id, context);
      }
    }
    id = await conversation.commit(async (tx) => {
      const state = await tx.doc(State);
      if (state.task === null) {
        admission(root, config);
        state.task = await tx.createTask(
          Loop,
          { root, config },
          { ownership: { kind: "conversation" } },
        );
      } else {
        const task = await tx.task(state.task as TaskId);
        if (
          JSON.stringify((task?.input as unknown as Input)?.config) !==
          JSON.stringify(config)
        )
          throw new Error(
            "Original Study contract changed; recovery cannot replace accepted inputs",
          );
        if (
          options.resume &&
          task?.state.status === "terminal" &&
          task.state.outcome.status !== "completed"
        ) {
          admission(root, config);
          state.task = await tx.createTask(
            Loop,
            {
              root,
              config,
              recovery: state.investigations.map((row) => row.task),
            },
            { ownership: { kind: "conversation" } },
          );
          event(root, "recovery.continued", {
            originalTask: task.id,
            newTask: state.task,
          });
        }
      }
      return state.task as TaskId<LoopResult>;
    }, context);
  } catch (error) {
    await opened.close();
    throw error;
  }
  const monitor = watchAuthorization(root, config, conversation, harness);
  try {
    await monitor.stop();
    const outcome = (await harness.waitForTask(id, context)).state.outcome;
    if (options.drainManager !== false) {
      let manager = (await harness.snapshot(State, context))?.manager;
      while (manager !== null && manager !== undefined) {
        await harness.waitForTask(manager as TaskId, context);
        const next = (await harness.snapshot(State, context))?.manager;
        if (monitor.stopped() || next === manager) break; // A failed Manager retains its pending input.
        manager = next;
      }
    }
    return outcome;
  } finally {
    monitor.close();
    await opened.close();
  }
}

export async function maintainKnowledge(
  root: string,
  options: { models?: Models; experiments?: ExperimentBackend } = {},
) {
  root = resolve(root);
  const opened = await openStudy(root, options);
  try {
    admission(root, opened.config);
    const conversation = await opened.harness.root(context);
    const monitor = watchAuthorization(
      root,
      opened.config,
      conversation,
      opened.harness,
    );
    try {
      const id = await conversation.commit(async (tx) => {
        const state = await tx.doc(State);
        if (!state.pending.length) return null;
        const existing =
          state.manager === null
            ? null
            : await tx.task(state.manager as TaskId);
        if (!existing || existing.state.status === "terminal")
          state.manager = await tx.createTask(
            opened.Manager,
            { root, config: opened.config, records: [...state.pending] },
            { ownership: { kind: "conversation" }, background: true },
          );
        return state.manager;
      }, context);
      await monitor.stop();
      return id === null
        ? { pending: [] }
        : (await opened.harness.waitForTask(id as TaskId, context)).state
            .outcome;
    } finally {
      monitor.close();
    }
  } finally {
    await opened.close();
  }
}

export async function studyStatus(root: string) {
  const opened = await openStudy(root, { readOnly: true });
  try {
    const state = await opened.harness.snapshot(State, context);
    return {
      state,
      task: state?.task
        ? await opened.harness.getTask(state.task as TaskId, context)
        : null,
      live: await opened.harness.inspect(context),
      usage: await opened.harness.usage(context),
    };
  } finally {
    await opened.close();
  }
}

/** Read-only configuration or a previously frozen call; never admits model or experiment work. */
export async function studyContext(
  root: string,
  conversation?: number,
  expand = false,
) {
  const config = readConfig(root);
  if (conversation === undefined)
    return {
      roles: previewDefinitions(config, expand),
      mounts: config.context?.mounts ?? {},
    };
  if (!Number.isSafeInteger(conversation) || conversation < 1)
    throw new Error("context requires a positive conversation ID");
  const opened = await openStudy(root, { readOnly: true });
  try {
    const binding = await opened.harness.snapshot(
      Binding,
      conversation as ConversationId,
      context,
    );
    if (!binding?.assembled)
      throw new Error("No frozen role context for this conversation");
    return previewContext(binding.assembled, expand);
  } finally {
    await opened.close();
  }
}
