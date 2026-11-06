import type { JsonValue } from "@earendil-works/chord";
import { isAbsolute, join, resolve } from "node:path";
import { arr, bool, obj, oneOf, str } from "@earendil-works/pi-schema";
import type {
  Plan,
  Receipt,
  ResearchRecord,
  ResearchTask,
  StudyConfig,
} from "./contract.ts";

export type Role = "plan" | "execute" | "review" | "manager";
export interface RolePolicy {
  instructions?: string;
  tools?: string[];
  readOnly?: string[];
  writable?: string[];
}
export interface ContextConfig {
  mounts?: Record<string, string>;
  roles?: Partial<Record<Role, RolePolicy>>;
}

export const planSchema = obj({
  question: str(),
  change: str(),
  rationale: str(),
  basedOn: arr(str()),
});
export const executeSchema = obj({ summary: str() });
export const reviewSchema = obj({
  interpretation: str(),
  limitations: arr(str()),
  knowledgeChanged: bool(),
  goal: oneOf(["achieved", "not-achieved", "unknown"]),
  continue: bool(),
  nextQuestion: str(),
});
export const notesSchema = obj({});

// Inputs and outputs are the controller's contract; policy selects tools and file access.
export const ROLE_DEFINITIONS = {
  plan: {
    inputs: [
      "goal",
      "constraints",
      "task",
      "candidateFiles",
      "publicInputs",
      "evaluation",
      "records",
    ],
    readOnly: ["workspace", "inputs", "knowledge"],
    writable: [],
    tools: ["read", "submit_plan"],
    output: { tool: "submit_plan", schema: planSchema },
  },
  execute: {
    inputs: [
      "goal",
      "constraints",
      "task",
      "plan",
      "candidateFiles",
      "publicInputs",
      "python",
    ],
    readOnly: ["inputs", "knowledge"],
    writable: ["workspace"],
    tools: [
      "read",
      "write",
      "edit",
      "bash",
      "run_experiment",
      "finish_execute",
    ],
    output: { tool: "finish_execute", schema: executeSchema },
  },
  review: {
    inputs: [
      "goal",
      "round",
      "maxRounds",
      "minimumRounds",
      "constraints",
      "publicInputs",
      "task",
      "plan",
      "execute",
      "records",
      "receipts",
      "evaluation",
    ],
    readOnly: ["workspace", "inputs", "knowledge", "records", "evidence"],
    writable: [],
    tools: ["read", "submit_review"],
    output: { tool: "submit_review", schema: reviewSchema },
  },
  manager: {
    inputs: ["records"],
    readOnly: ["knowledge", "records"],
    writable: ["workspace"],
    tools: ["read", "write", "edit", "bash", "publish_notes"],
    output: { tool: "publish_notes", schema: notesSchema },
  },
} satisfies Record<Role, object>;
const resources = ["workspace", "inputs", "knowledge", "records", "evidence"];
const toolCeilings: Record<Role, string[]> = {
  plan: ["read", "bash", "submit_plan"],
  execute: [...ROLE_DEFINITIONS.execute.tools],
  review: ["read", "bash", "submit_review"],
  manager: [...ROLE_DEFINITIONS.manager.tools],
};
const resourceCeilings: Record<Role, string[]> = {
  plan: ["workspace", "inputs", "knowledge"],
  execute: ["workspace", "inputs", "knowledge"],
  review: resources,
  manager: ["workspace", "knowledge", "records"],
};
const roles = Object.keys(ROLE_DEFINITIONS) as Role[];
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
function fail(field: string, message: string): never {
  throw new Error(`context.${field}: ${message}`);
}
function names(value: unknown, field: string): asserts value is string[] {
  if (
    !Array.isArray(value) ||
    value.some((item) => typeof item !== "string") ||
    new Set(value).size !== value.length
  )
    fail(field, "expected distinct names");
}

export function validateContextConfig(value: unknown) {
  if (value === undefined) return;
  if (!object(value)) fail("", "expected an object");
  for (const key of Object.keys(value))
    if (!["mounts", "roles"].includes(key))
      fail(key, "unknown configuration field");
  if (value.mounts !== undefined && !object(value.mounts))
    fail("mounts", "expected named absolute read-only paths");
  const mounts = value.mounts ?? {};
  if (!object(mounts))
    fail("mounts", "expected named absolute read-only paths");
  for (const [name, path] of Object.entries(mounts))
    if (
      !/^[a-z][a-z0-9_-]*$/.test(name) ||
      resources.includes(name) ||
      typeof path !== "string" ||
      !isAbsolute(path) ||
      resolve(path) === "/"
    )
      fail(
        `mounts.${name}`,
        "use a non-reserved name and a bounded absolute read-only path",
      );
  if (value.roles !== undefined && !object(value.roles))
    fail("roles", "expected role policies");
  const policies = value.roles ?? {};
  if (!object(policies)) fail("roles", "expected role policies");
  for (const [role, policy] of Object.entries(policies)) {
    if (!roles.includes(role as Role)) fail(`roles.${role}`, "unknown role");
    if (!object(policy)) fail(`roles.${role}`, "expected a role policy");
    for (const key of Object.keys(policy))
      if (!["instructions", "tools", "readOnly", "writable"].includes(key))
        fail(`roles.${role}.${key}`, "unknown configuration field");
    if (
      policy.instructions !== undefined &&
      typeof policy.instructions !== "string"
    )
      fail(
        `roles.${role}.instructions`,
        "expected additional text instructions",
      );
    const defaults = ROLE_DEFINITIONS[role as Role];
    for (const key of ["tools", "readOnly", "writable"] as const)
      if (policy[key] !== undefined) names(policy[key], `roles.${role}.${key}`);
    const tools = (policy.tools ?? defaults.tools) as string[];
    if (tools.some((name) => !toolCeilings[role as Role].includes(name)))
      fail(
        `roles.${role}.tools`,
        "unknown tool or tool exceeds this role's authority",
      );
    for (const required of [
      defaults.output.tool,
      ...(role === "execute" ? ["run_experiment"] : []),
    ])
      if (!tools.includes(required))
        fail(
          `roles.${role}.tools`,
          `required tool ${required} cannot be removed`,
        );
    const readOnly = (policy.readOnly ?? defaults.readOnly) as string[];
    const writable = (policy.writable ?? defaults.writable) as string[];
    if (
      readOnly.some(
        (name) =>
          !resourceCeilings[role as Role].includes(name) &&
          !Object.hasOwn(mounts, name),
      )
    )
      fail(`roles.${role}.readOnly`, "unknown or unauthorized resource");
    if (
      writable.some((name) => name !== "workspace") ||
      JSON.stringify(writable) !== JSON.stringify(defaults.writable)
    )
      fail(
        `roles.${role}.writable`,
        "write access is fixed to the Execute/Manager workspace; other roles are read-only",
      );
    if (!readOnly.includes("workspace") && !writable.includes("workspace"))
      fail(
        `roles.${role}.readOnly`,
        "the role's working directory must remain readable",
      );
    if (readOnly.some((name) => writable.includes(name)))
      fail(`roles.${role}.readOnly`, "resource is also writable");
    if (role === "review" && !readOnly.includes("evidence"))
      fail(
        `roles.${role}.readOnly`,
        "independent Review requires candidate snapshots and raw results (evidence)",
      );
  }
}

export function roleDefinition(config: StudyConfig, role: Role) {
  validateContextConfig(config.context);
  const defaults = ROLE_DEFINITIONS[role];
  const policy = config.context?.roles?.[role];
  return structuredClone({
    role,
    instructions:
      `You are the ${role} of an evidence-driven research study. Follow the human goal and constraints. Use the role's finish tool rather than merely writing a final chat response. Never treat self-reported numbers as measurements.` +
      (policy?.instructions
        ? `\nAdditional role instructions:\n${policy.instructions}`
        : ""),
    inputs: defaults.inputs,
    tools: policy?.tools ?? defaults.tools,
    readOnly: policy?.readOnly ?? defaults.readOnly,
    writable: policy?.writable ?? defaults.writable,
    output: defaults.output,
  });
}

export type RoleRequest = { workspace: string; records: ResearchRecord[] } & (
  | { role: "plan"; task: ResearchTask }
  | { role: "execute"; task: ResearchTask; plan: Plan }
  | {
      role: "review";
      round: number;
      task: ResearchTask;
      plan: Plan;
      execute: string;
      receipts: Receipt[];
    }
  | { role: "manager" }
);
export type RoleContext = {
  version: 1;
  role: Role;
  workspace: string;
  instructions: string;
  inputs: { [name: string]: JsonValue };
  prompt: string;
  tools: string[];
  output: { tool: string; schema: { [key: string]: JsonValue } };
  access: { resource: string; mode: "read" | "write"; paths: string[] }[];
};

export function contextAccess(context: RoleContext) {
  const paths = (mode: "read" | "write") => [
    ...new Set(
      context.access
        .filter((row) => row.mode === mode)
        .flatMap((row) => row.paths),
    ),
  ];
  return { readOnly: paths("read"), writable: paths("write") };
}

/** The returned JSON is frozen into the owned conversation and used by execution and preview. */
export function assembleContext(
  root: string,
  config: StudyConfig,
  request: RoleRequest,
): RoleContext {
  const definition = roleDefinition(config, request.role);
  const receipts = request.role === "review" ? request.receipts : [];
  if (
    [...receipts, ...request.records.flatMap((record) => record.receipts)].some(
      (receipt) => receipt.feedback !== "development",
    )
  )
    throw new Error(
      "Restricted/final-only feedback cannot enter learning or knowledge",
    );
  const paths: Record<string, string[]> = {
    workspace: [request.workspace],
    inputs: [config.inputs.root],
    knowledge: [join(root, "knowledge")],
    records: request.records.map((record) =>
      join(root, "records", record.id + ".json"),
    ),
    evidence: [
      ...receipts,
      ...request.records.flatMap((record) => record.receipts),
    ].flatMap((receipt) => [receipt.candidatePath, receipt.rawResult]),
    ...Object.fromEntries(
      Object.entries(config.context?.mounts ?? {}).map(([name, path]) => [
        name,
        [path],
      ]),
    ),
  };
  const access: RoleContext["access"] = [
    ...definition.readOnly.map((resource) => ({
      resource,
      mode: "read" as const,
      paths: paths[resource],
    })),
    ...definition.writable.map((resource) => ({
      resource,
      mode: "write" as const,
      paths: paths[resource],
    })),
  ];
  const knowledge = definition.readOnly.includes("knowledge");
  const instructions =
    definition.instructions +
    (knowledge
      ? ` Published knowledge is at ${join(root, "knowledge", "research.md")}; a fresh read can see updates during this conversation.`
      : "") +
    `\nFile access grants: ${JSON.stringify(access)}. System runtime files and private scratch are supplied by the sandbox; these grants do not authorize new experiments.`;
  // Do not expose trusted prediction/scoring command paths in role inputs.
  const publicEvaluation = {
    id: config.evaluation.id,
    version: config.evaluation.version,
    inputVersion: config.inputs.version,
    description: config.evaluation.description ?? "",
  };
  let inputs: Record<string, unknown>;
  let prompt: string;
  switch (request.role) {
    case "plan":
      inputs = {
        goal: config.goal,
        constraints: config.constraints,
        task: request.task,
        candidateFiles: config.source.files,
        publicInputs: config.inputs.root,
        evaluation: publicEvaluation,
        records: request.records,
      };
      prompt = `Goal: ${config.goal}
Human constraints: ${JSON.stringify(config.constraints)}
Task: ${JSON.stringify(request.task)}
Candidate files: ${JSON.stringify(config.source.files)}
Public inputs: ${config.inputs.root}
Fixed evaluation: ${config.evaluation.id}/${config.evaluation.version}, input ${config.inputs.version}. ${config.evaluation.description ?? ""} Keep it fixed.
Prior independently reviewed evidence: ${JSON.stringify(request.records)}
Read the permitted candidate/inputs${knowledge ? " and knowledge" : ""}. Choose a concrete investigation; if prior records exist, explain specifically how their evidence/limits change your next code edit and cite record IDs in basedOn. Freeze it with submit_plan.`;
      break;
    case "execute":
      inputs = {
        goal: config.goal,
        constraints: config.constraints,
        task: request.task,
        plan: request.plan,
        candidateFiles: config.source.files,
        publicInputs: config.inputs.root,
        python: config.python,
      };
      prompt = `Goal: ${config.goal}
Immutable human constraints: ${JSON.stringify(config.constraints)}
Accepted task: ${JSON.stringify(request.task)}
Frozen Plan: ${JSON.stringify(request.plan)}
Read and edit the candidate files ${JSON.stringify(config.source.files)}, run permitted CPU checks and correct mistakes. Public input files are at ${config.inputs.root}; file operations are limited to the frozen path grants. Python executable: ${config.python}. Actually implement the Plan, then call run_experiment exactly for this authorized round to obtain trusted evaluation. Do not fake a metric or change evaluator/data. Your operation slot is fixed; rerunning cannot create more jobs. After inspecting the receipt call finish_execute with an honest public summary.`;
      break;
    case "review":
      inputs = {
        goal: config.goal,
        round: request.round + 1,
        maxRounds: config.maxRounds,
        minimumRounds: config.minimumRounds ?? 1,
        constraints: config.constraints,
        publicInputs: config.inputs.root,
        task: request.task,
        plan: request.plan,
        execute: request.execute,
        records: request.records,
        receipts: request.receipts,
        evaluation: publicEvaluation,
      };
      prompt = `You are an independent fresh Review session, with no hidden Execute chat.
Goal: ${config.goal}
Current measured round: ${request.round + 1}; authorized limit: ${config.maxRounds}; human-required minimum: ${config.minimumRounds ?? 1}.
Human constraints: ${JSON.stringify(config.constraints)}
Public input directory: ${config.inputs.root}
Actual task: ${JSON.stringify(request.task)}
Accepted Plan: ${JSON.stringify(request.plan)}
Public Execute summary (not measurement authority): ${request.execute}
Prior independently reviewed records: ${JSON.stringify(request.records)}
Trusted receipts: ${JSON.stringify(request.receipts)}
Fixed evaluation: ${config.evaluation.id}/${config.evaluation.version}, inputs ${config.inputs.version}. ${config.evaluation.description ?? ""} Read the actual candidate snapshots and raw results. Separate execution completion, validity, knowledge change, goal achievement/unknown and continuation. Budget or round exhaustion is not goal achievement. Record specific limitations and a next research question using submit_review.`;
      break;
    case "manager":
      inputs = { records: request.records };
      prompt = `Read the ${knowledge ? "latest published knowledge and the " : ""}reviewed records below. Read and edit research.md in your private workspace, then call publish_notes. Keep negative results and limitations. Do not rewrite accepted Plans or raw measurements.
${JSON.stringify(request.records)}`;
      break;
  }
  return JSON.parse(
    JSON.stringify({
      version: 1,
      role: request.role,
      workspace: request.workspace,
      instructions,
      inputs,
      prompt,
      tools: definition.tools,
      output: definition.output,
      access,
    }),
  );
}

export function previewContext(context: RoleContext, expand = false) {
  if (expand) return structuredClone(context);
  const {
    instructions: _instructions,
    inputs,
    prompt: _prompt,
    ...manifest
  } = context;
  return { ...structuredClone(manifest), inputs: Object.keys(inputs) };
}
export function previewDefinitions(config: StudyConfig, expand = false) {
  return roles.map((role) => {
    const definition = roleDefinition(config, role);
    if (expand) return definition;
    const { instructions: _instructions, ...manifest } = definition;
    return manifest;
  });
}
