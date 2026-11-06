import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import type { ResearchRecord, StudyConfig } from "./contract.ts";
import { validateContextConfig } from "./context.ts";

export function writeJson(path: string, value: unknown) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(value, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
}

export function readConfig(root: string): StudyConfig {
  if (
    !existsSync(join(root, "IDENTITY")) ||
    ["runtime.sqlite3", "runtime.json", "state.json"].some((file) =>
      existsSync(join(root, file)),
    )
  )
    throw new Error(
      "Legacy or unsupported Study; execution is refused. Use history on a consistent read-only snapshot and reconcile original effects before creating a new Study.",
    );
  const config = JSON.parse(
    readFileSync(join(root, "study.json"), "utf8"),
  ) as StudyConfig;
  validateConfig(config);
  if (readFileSync(join(root, "IDENTITY"), "utf8") !== config.id)
    throw new Error("Study identity does not match");
  return config;
}

export function validateConfig(config: StudyConfig) {
  validateContextConfig(config.context);
  if (
    config.version !== 1 ||
    !config.id ||
    !config.goal ||
    !config.authorization?.id ||
    !Number.isFinite(config.authorization.expiresAt)
  )
    throw new Error("Invalid durable Study configuration");
  if (
    config.execution &&
    (!config.execution.id ||
      !config.execution.version ||
      !config.execution.identity)
  )
    throw new Error("Freeze the exact execution backend version and identity");
  if (
    config.inputs.feedback !== "development" ||
    config.evaluation.feedback !== "development"
  )
    throw new Error(
      "final-only inputs/evaluation are not admitted to research or knowledge",
    );
  if (
    !Number.isInteger(config.maxRounds) ||
    config.maxRounds < 1 ||
    config.authorization.slots.length < config.maxRounds ||
    new Set(config.authorization.slots).size !==
      config.authorization.slots.length
  )
    throw new Error(
      "Each bounded round needs a distinct pre-authorized operation slot",
    );
  if (
    config.concurrency !== undefined &&
    (config.concurrency === null ||
      typeof config.concurrency !== "object" ||
      Array.isArray(config.concurrency))
  )
    throw new Error("Concurrency must be an object of positive integer limits");
  if (
    config.concurrency &&
    (!Number.isSafeInteger(config.concurrency.investigations ?? 1) ||
      (config.concurrency.investigations ?? 1) < 1 ||
      !Number.isSafeInteger(config.concurrency.experiments ?? 1) ||
      (config.concurrency.experiments ?? 1) < 1)
  )
    throw new Error("Concurrency limits must be positive safe integers");
  if (
    config.tasks.length > config.maxRounds ||
    new Set(config.tasks.map((task) => task.id)).size !== config.tasks.length
  )
    throw new Error(
      "Configured investigations require unique IDs and authorized rounds",
    );
  for (const [index, task] of config.tasks.entries())
    if (
      task.basedOn.some(
        (id) =>
          !/^r[0-9]{4,}$/.test(id) ||
          Number(id.slice(1)) < 1 ||
          Number(id.slice(1)) > index ||
          id !== "r" + String(Number(id.slice(1))).padStart(4, "0"),
      )
    )
      throw new Error(
        "Investigation basedOn must cite a distinct earlier round record ID",
      );
  if (
    !Number.isInteger(config.minimumRounds ?? 1) ||
    (config.minimumRounds ?? 1) < 1 ||
    (config.minimumRounds ?? 1) > config.maxRounds
  )
    throw new Error(
      "Required reviewed rounds must fit the authorized round limit",
    );
  if (
    !Number.isFinite(config.authorization.jobSeconds) ||
    config.authorization.jobSeconds <= 0
  )
    throw new Error("CPU job time must be explicitly bounded");
  if (
    !config.model.provider ||
    !config.model.id ||
    !["off", "medium"].includes(config.model.thinking)
  )
    throw new Error("Configure an exact provider/model and thinking setting");
  if (
    !isAbsolute(config.source.root) ||
    !isAbsolute(config.inputs.root) ||
    !isAbsolute(config.python) ||
    config.source.files.length === 0
  )
    throw new Error(
      "Source, inputs and Python must use explicit absolute paths",
    );
  for (const file of config.source.files)
    if (
      isAbsolute(file) ||
      file.split(/[\\/]/).some((part) => part === ".." || part === "")
    )
      throw new Error("Candidate paths must stay within the source root");
  if (
    (!config.execution &&
      (config.evaluation.predictCommand.length === 0 ||
        config.evaluation.command.length === 0)) ||
    !config.inputs.version ||
    !config.evaluation.id ||
    !config.evaluation.version
  )
    throw new Error(
      "Freeze prediction/scoring commands and input/evaluation versions",
    );
}

export function admission(root: string, config: StudyConfig) {
  if (existsSync(join(root, "STOP")))
    throw new Error(
      "STOP is set; only inspect, reconcile or cancel existing effects",
    );
  if (Date.now() >= config.authorization.expiresAt)
    throw new Error("Original authorization deadline expired");
}

export function initialize(root: string, config: StudyConfig) {
  if (existsSync(root))
    throw new Error(
      "Study root already exists; use status for this runtime or history on a consistent read-only legacy snapshot. Initialization never overwrites or migrates an existing root.",
    );
  validateConfig(config);
  root = resolve(root);
  // Resolve supplied paths once; recovery never refreshes resources or relocates them.
  for (const path of [config.source.root, config.inputs.root, config.python])
    realpathSync(path);
  for (const [name, path] of Object.entries(config.context?.mounts ?? {}))
    config.context!.mounts![name] = realpathSync(path);
  validateContextConfig(config.context);
  mkdirSync(root, { recursive: false, mode: 0o700 });
  for (const folder of [
    "workspaces",
    "records",
    "knowledge",
    "manager",
    "rounds",
  ])
    mkdirSync(join(root, folder));
  writeFileSync(join(root, "IDENTITY"), config.id, { flag: "wx", mode: 0o600 });
  writeJson(join(root, "study.json"), config);
  writeFileSync(
    join(root, "knowledge", "research.md"),
    "# Research knowledge\n\nNo reviewed research has been published.\n",
    { flag: "wx" },
  );
  return root;
}

export function readRecord(root: string, id: string): ResearchRecord {
  if (!/^r[0-9]{4,}$/.test(id)) throw new Error("Invalid record identity");
  return JSON.parse(readFileSync(join(root, "records", id + ".json"), "utf8"));
}

export function publishRecord(root: string, record: ResearchRecord) {
  const path = join(root, "records", record.id + ".json");
  if (existsSync(path)) {
    if (JSON.stringify(readRecord(root, record.id)) !== JSON.stringify(record))
      throw new Error("A different immutable Review already exists");
  } else writeJson(path, record);
}

export function publishKnowledge(root: string, id: string, content: string) {
  const path = join(root, "knowledge", "research.md");
  const published = `<!-- publication:${id} -->\n` + content;
  if (readFileSync(path, "utf8") === published) return false;
  const draft = join(root, "knowledge", `.draft-${id}.md`);
  writeFileSync(draft, published, { mode: 0o600 });
  renameSync(draft, path);
  return true;
}

export function readRegular(path: string) {
  if (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink())
    throw new Error("Expected a regular draft/source file, not a symlink");
  return readFileSync(path, "utf8");
}

export function event(root: string, name: string, detail: object = {}) {
  // Diagnostics only. Framework checkpoints and research records own their state.
  const path = join(root, "events.jsonl");
  try {
    writeFileSync(
      path,
      JSON.stringify({ at: new Date().toISOString(), event: name, ...detail }) +
        "\n",
      { flag: "a", mode: 0o600 },
    );
  } catch {
    /* Diagnostics never decide publication or execution success. */
  }
}
