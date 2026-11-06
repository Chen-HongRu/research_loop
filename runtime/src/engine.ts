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
import { arr, bool, obj, oneOf, str } from "@earendil-works/pi-schema";
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
type Role = "plan" | "execute" | "review" | "manager";
const Binding = defineDoc<{
  role: string;
  workspace: string;
  writable: boolean;
  records: string[];
  slot: string;
  readOnly: string[];
}>({
  kind: "research.role",
  version: 1,
  scope: "conversation",
  history: "latest",
  fork: "initial",
  initial: () => ({
    role: "",
    workspace: "",
    writable: false,
    records: [],
    slot: "",
    readOnly: [],
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
    publications: [],
  }),
  checkpointWhen: () => false,
});

const planSchema = obj({
  question: str(),
  change: str(),
  rationale: str(),
  basedOn: arr(str()),
});
const reviewSchema = obj({
  interpretation: str(),
  limitations: arr(str()),
  knowledgeChanged: bool(),
  goal: oneOf(["achieved", "not-achieved", "unknown"]),
  continue: bool(),
  nextQuestion: str(),
});
type Input = { root: string; config: StudyConfig; resume?: RoundState };
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
  const records = ids.map((id) => readRecord(root, id));
  if (
    records.some((record) =>
      record.receipts.some((receipt) => receipt.feedback !== "development"),
    )
  )
    throw new Error(
      "Restricted/final-only feedback cannot enter learning or knowledge",
    );
  return records;
}

async function ask<I, S, R>(
  root: string,
  runtime: TaskRuntime<I, S, R, object>,
  id: ConversationId,
  requestId: string,
  prompt: string,
) {
  const binding = await runtime.snapshot(Binding, id, context);
  event(root, "role.active", { role: binding?.role, conversation: id });
  const conversation = (await runtime.conversation(id, context))!;
  const submission = await conversation.submit(
    { type: "input", requestId, content: prompt },
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
  Loop: Task<Input, RoundState, LoopResult, object>;
  Manager: Task<Input & { records: string[] }, ManagerState, null, object>;
  State: typeof State;
  experiments: Pick<ExperimentBackend, "run" | "readReceipts">;
  close(): Promise<void>;
}> {
  root = resolve(root);
  const config = readConfig(root);
  const readOnly = options.readOnly ?? false;
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
        if (answer.value === null) answer.value = args;
      }, ctx);
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
    parameters: obj({ summary: str() }),
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
      const receipt = await experiments.run(
        root,
        config,
        role.workspace,
        role.slot,
      );
      return { output: [{ type: "text", text: JSON.stringify(receipt) }] };
    },
  });
  const notes = defineTool({
    name: "publish_notes",
    description:
      "Finish editing your research.md draft for atomic publication when this Manager conversation succeeds. Reviewed records remain authoritative; do not invent measurements.",
    parameters: obj({}),
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

  const toolNames: Record<Role, string[]> = {
    plan: ["read", "submit_plan"],
    execute: [
      "read",
      "write",
      "edit",
      "bash",
      "run_experiment",
      "finish_execute",
    ],
    review: ["read", "submit_review"],
    manager: ["read", "write", "edit", "bash", "publish_notes"],
  };
  const allTools = [
    ...CodingTools.tools!,
    finishPlan,
    finishExecute,
    finishReview,
    experiment,
    notes,
  ];
  function reviewPaths(receipts: Receipt[], ids: string[]) {
    return [
      config.inputs.root,
      join(root, "knowledge"),
      ...ids.map((id) => join(root, "records", id + ".json")),
      ...[
        ...receipts,
        ...previous(root, ids).flatMap((record) => record.receipts),
      ].flatMap((receipt) => [receipt.candidatePath, receipt.rawResult]),
    ];
  }
  async function roleInCommit(
    tx: Parameters<Parameters<Harness["commit"]>[0]>[0],
    owner: TaskId,
    role: Role,
    workspace: string,
    records: string[],
    slot: string,
    readOnlyPaths: string[],
  ) {
    const conversation = await tx.createConversation({
      ownership: { kind: "task", taskId: owner },
    });
    Object.assign(await tx.doc(Binding, conversation.id), {
      role,
      workspace,
      records,
      slot,
      readOnly: readOnlyPaths,
      writable: role === "execute" || role === "manager",
    });
    await configure(tx, conversation.id, {
      cwd: workspace,
      model: { provider: config.model.provider, modelId: config.model.id },
      thinkingLevel: config.model.thinking,
      modelTools: allTools.filter((tool) =>
        toolNames[role].includes(tool.name),
      ),
      instructions: `You are the ${role} of an evidence-driven research study. Follow the human goal and constraints. Use the role's finish tool rather than merely writing a final chat response. Never treat self-reported numbers as measurements. Published knowledge is at ${join(root, "knowledge", "research.md")}; a fresh read can see updates during this conversation.`,
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
                conversation: await roleInCommit(
                  tx,
                  task.id,
                  "manager",
                  workspace,
                  task.input.records,
                  "",
                  [
                    join(root, "knowledge"),
                    ...task.input.records.map((id) =>
                      join(root, "records", id + ".json"),
                    ),
                  ],
                ),
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
            `Read the latest published knowledge and the reviewed records below. Read and edit research.md in your private workspace, then call publish_notes. Keep negative results and limitations. Do not rewrite accepted Plans or raw measurements.
${JSON.stringify(previous(root, task.input.records))}`,
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

  const Loop = defineTask<Input, RoundState, LoopResult>({
    name: "research.loop",
    version: 1,
    initial: (input) => input.resume ?? { phase: "plan", round: 0 },
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
              reviewer: await roleInCommit(
                tx,
                task.id,
                "review",
                cp.workspace,
                [],
                "",
                reviewPaths(receipts, cp.plan.basedOn),
              ),
            },
          }),
          ctx,
        );
      },
      plan: async (task, runtime, ctx) => {
        admission(root, config);
        const state = (await runtime.snapshot(State, ctx))!;
        const human = config.tasks[task.state.checkpoint.round];
        const selected: ResearchTask = human ?? {
          id: recordId(task.state.checkpoint.round),
          source: "autonomous",
          question:
            "Select a follow-up investigation from the reviewed evidence",
          constraints: [],
          basedOn: [...state.records],
        };
        const workspace = join(
          root,
          "workspaces",
          recordId(task.state.checkpoint.round),
        );
        mkdirSync(workspace, { recursive: true });
        const preceding = state.records.length
          ? readRecord(root, state.records.at(-1)!)
          : null;
        const source =
          preceding?.receipts.find((receipt) => receipt.valid)?.candidatePath ??
          config.source.root;
        for (const file of config.source.files) {
          mkdirSync(dirname(join(workspace, file)), { recursive: true });
          writeFileSync(join(workspace, file), readRegular(join(source, file)));
        }
        await runtime.commit(
          async (tx) => ({
            status: "running",
            checkpoint: {
              phase: "awaitPlan",
              round: task.state.checkpoint.round,
              task: selected,
              conversation: await roleInCommit(
                tx,
                task.id,
                "plan",
                workspace,
                [...state.records],
                "",
                [workspace, config.inputs.root, join(root, "knowledge")],
              ),
            },
          }),
          ctx,
        );
      },
      awaitPlan: async (task, runtime, ctx) => {
        const cp = task.state.checkpoint;
        const state = (await runtime.snapshot(State, ctx))!;
        const plan = (await ask(
          root,
          runtime,
          cp.conversation,
          "plan",
          `Goal: ${config.goal}
Human constraints: ${JSON.stringify(config.constraints)}
Task: ${JSON.stringify(cp.task)}
Candidate files: ${JSON.stringify(config.source.files)}
Public inputs: ${config.inputs.root}
Fixed evaluation: ${config.evaluation.id}/${config.evaluation.version}, input ${config.inputs.version}. ${config.evaluation.description ?? ""} Keep it fixed.
Prior independently reviewed evidence: ${JSON.stringify(previous(root, state.records))}
Read candidate/inputs and knowledge. Choose a concrete investigation; if prior records exist, explain specifically how their evidence/limits change your next code edit and cite record IDs in basedOn. Freeze it with submit_plan.`,
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
        const state = (await runtime.snapshot(State, ctx))!;
        await runtime.commit(
          async (tx) => ({
            status: "running",
            checkpoint: {
              phase: "awaitExecute",
              round: cp.round,
              plan: cp.plan,
              task: cp.task,
              planner: cp.planner,
              workspace,
              executor: await roleInCommit(
                tx,
                task.id,
                "execute",
                workspace,
                [...state.records],
                config.authorization.slots[cp.round],
                [config.inputs.root, join(root, "knowledge")],
              ),
            },
          }),
          ctx,
        );
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
          if (
            JSON.stringify(JSON.parse(readFileSync(path, "utf8"))) !==
            JSON.stringify(accepted)
          )
            throw new Error("Accepted experiment identity changed");
        } else writeJson(path, accepted);
        const summary = await ask(
          root,
          runtime,
          cp.executor,
          "execute",
          `Goal: ${config.goal}
Immutable human constraints: ${JSON.stringify(config.constraints)}
Accepted task: ${JSON.stringify(cp.task)}
Frozen Plan: ${JSON.stringify(cp.plan)}
Read and edit the candidate files ${JSON.stringify(config.source.files)}, run bash CPU checks and correct mistakes. Public input files are at ${config.inputs.root}; canonical inputs, hidden evaluation and model credentials are protected by the OS. Python executable: ${config.python}. Actually implement the Plan, then call run_experiment exactly for this authorized round to obtain trusted evaluation. Do not fake a metric or change evaluator/data. Your operation slot is fixed; rerunning cannot create more jobs. After inspecting the receipt call finish_execute with an honest public summary.`,
        );
        const receipts = experiments
          .readReceipts(root, config)
          .filter(
            (receipt) => receipt.slot === config.authorization.slots[cp.round],
          );
        if (receipts.length === 0)
          throw new Error(
            "Execute produced no trusted experiment receipt; self-reports are not measurements",
          );
        const readOnlyPaths = reviewPaths(receipts, cp.plan.basedOn);
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
              reviewer: await roleInCommit(
                tx,
                task.id,
                "review",
                cp.workspace,
                [],
                "",
                readOnlyPaths,
              ),
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
          `You are an independent fresh Review session, with no hidden Execute chat.
Goal: ${config.goal}
Current measured round: ${cp.round + 1}; authorized limit: ${config.maxRounds}; human-required minimum: ${config.minimumRounds ?? 1}.
Human constraints: ${JSON.stringify(config.constraints)}
Public input directory: ${config.inputs.root}
Actual task: ${JSON.stringify(cp.task)}
Accepted Plan: ${JSON.stringify(cp.plan)}
Public Execute summary (not measurement authority): ${cp.execute}
Prior independently reviewed records: ${JSON.stringify(previous(root, cp.plan.basedOn))}
Trusted receipts: ${JSON.stringify(cp.receipts)}
Fixed evaluation: ${config.evaluation.id}/${config.evaluation.version}, inputs ${config.inputs.version}. ${config.evaluation.description ?? ""} Read the actual candidate snapshots and raw results. Separate execution completion, validity, knowledge change, goal achievement/unknown and continuation. Budget or round exhaustion is not goal achievement. Record specific limitations and a next research question using submit_review.`,
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
            cp.record.review.goal === "achieved" ||
            !cp.record.review.continue ||
            cp.round + 1 >= config.maxRounds
          )
            return {
              status: "terminal",
              outcome: {
                status: "completed",
                result: {
                  records: [...state.records],
                  goal: cp.record.review.goal,
                  stopped:
                    cp.record.review.goal === "achieved"
                      ? "reviewed goal achieved"
                      : !cp.record.review.continue
                        ? "reviewed stop"
                        : "authorized round limit",
                },
              },
            };
          return {
            status: "running",
            checkpoint: { phase: "plan", round: cp.round + 1 },
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

  const registry = createRegistry();
  registry.install(CodingTools);
  registry.install(
    defineExtension({
      name: "research",
      tools: [finishPlan, finishExecute, finishReview, experiment, notes],
      tasks: [Loop, Manager],
      hooks: [
        hook(ToolTask, {
          beforeTool(call) {
            admission(root, config);
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
        if (!binding?.role) throw new Error("No role workspace binding");
        if (!environments.has(target.conversationId))
          environments.set(
            target.conversationId,
            await createEnvironment({
              id: String(target.conversationId),
              workspace: binding.workspace,
              readOnly: binding.readOnly,
              writable: binding.writable ? [binding.workspace] : [],
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
  if (!readOnly) {
    const state = await harness.snapshot(State, context);
    if (state?.task) {
      const original = await harness.getTask(state.task as TaskId, context);
      if (
        JSON.stringify((original?.input as unknown as Input)?.config) !==
        JSON.stringify(config)
      ) {
        await harness.close(context);
        throw new Error(
          "Original Study contract changed; authorization cannot be refreshed by editing configuration",
        );
      }
    }
  }
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
      const original = state?.task
        ? await harness.getTask(state.task as TaskId, context)
        : null;
      if (original && original.state.status !== "terminal") {
        const round = state!.records.length;
        if (
          !opened.experiments
            .readReceipts(root, config)
            .some(
              (receipt) => receipt.slot === config.authorization.slots[round],
            )
        )
          throw new Error(
            "Collect the bound original job before resume; no Execute replay is permitted",
          );
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
          const round = state.records.length;
          const id = recordId(round);
          const resume: RoundState = existsSync(
            join(root, "records", id + ".json"),
          )
            ? { phase: "record", round, record: readRecord(root, id) }
            : {
                phase: "reconciled",
                experiment: JSON.parse(
                  readFileSync(join(root, "rounds", id + ".json"), "utf8"),
                ) as SavedExperiment,
              };
          state.task = await tx.createTask(
            Loop,
            { root, config, resume },
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
