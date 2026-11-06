import {
  accessSync,
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  opendirSync,
  readSync,
  realpathSync,
} from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";

const LIMIT = 256;
const TEXT_LIMIT = 4 * 1024 * 1024;
const REQUIREMENT =
  "Provide a complete, consistent read-only snapshot. Do not change permissions on an active original Study. SQLite snapshots must retain runtime.sqlite3, runtime.sqlite3-wal and runtime.sqlite3-shm together; copying only the main database is insufficient.";
const NOTES = [
  "Original historical records and references only; no translation into new measurements or authorization.",
  "Current deployment/job state is unknown. A null or missing job_id is not proof that no job was submitted.",
  "No referenced artifact, dataset, checkpoint, workspace or external path is followed automatically.",
  "At most 256 metadata rows/entries per table or invocation directory are returned; truncated inventories are not exhaustive.",
];
const ROOT_REFS = [
  "goal.md",
  "study.json",
  "state.json",
  "runtime.json",
  "research.md",
  "observations.json",
  "frontiers.json",
  "research_inputs.json",
  "execution-backend.json",
  "STOP",
  "iterations",
  "tasks",
  "invocations",
  "control",
  "artifacts",
  "workspace",
  "knowledge",
  "receipts",
  "code",
  "runtime.sqlite3",
  "runtime.sqlite3-wal",
  "runtime.sqlite3-shm",
];

function readonly(path: string, directory: boolean): void {
  const info = lstatSync(path);
  if (
    info.isSymbolicLink() ||
    (directory ? !info.isDirectory() : !info.isFile())
  )
    throw new Error(
      "History paths must be original regular files/directories, never symlinks or special files",
    );
  if ((info.mode & 0o222) !== 0)
    throw new Error(`Writable history path refused. ${REQUIREMENT}`);
  let writable = false;
  try {
    accessSync(path, constants.W_OK);
    writable = true;
  } catch {
    /* No effective write grant. */
  }
  if (writable)
    throw new Error(`History path is effectively writable. ${REQUIREMENT}`);
}

function rootPath(root: string): string {
  const absolute = resolve(root);
  readonly(absolute, true);
  return realpathSync(absolute);
}

function pathInside(root: string, relative: string): string {
  if (
    typeof relative !== "string" ||
    !relative ||
    isAbsolute(relative) ||
    /[\\\0]/.test(relative)
  )
    throw new Error(
      "History file requires a safe relative path inside the snapshot",
    );
  const parts = relative.split("/");
  if (parts.some((part) => !part || part === "." || part === ".."))
    throw new Error(
      "History file requires a safe relative path inside the snapshot",
    );
  let current = root;
  for (const [index, part] of parts.entries()) {
    current = join(current, part);
    readonly(current, index < parts.length - 1);
  }
  return current;
}

/** Exact original UTF-8 text, explicitly requested; no interpretation or rewrite. */
export function readHistoryFile(root: string, relative: string): string {
  const canonical = rootPath(root);
  const path = pathInside(canonical, relative);
  const fd = openSync(
    path,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || (info.mode & 0o222) !== 0 || info.size > TEXT_LIMIT)
      throw new Error(
        "Explicit history text must be a regular file no larger than 4 MiB",
      );
    const buffer = Buffer.alloc(Math.min(TEXT_LIMIT + 1, info.size + 1));
    let length = 0;
    while (length < buffer.length) {
      const count = readSync(
        fd,
        buffer,
        length,
        buffer.length - length,
        length,
      );
      if (!count) break;
      length += count;
    }
    const after = fstatSync(fd);
    if (
      length !== info.size ||
      after.mtimeMs !== info.mtimeMs ||
      after.ctimeMs !== info.ctimeMs
    )
      throw new Error("Original history document changed during reading");
    if (length > TEXT_LIMIT || buffer.subarray(0, length).includes(0))
      throw new Error("Requested history file is not bounded text");
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      buffer.subarray(0, length),
    );
  } finally {
    closeSync(fd);
  }
}

function metadata(path: string) {
  const info = lstatSync(path);
  return {
    kind: info.isSymbolicLink()
      ? "symlink"
      : info.isDirectory()
        ? "directory"
        : info.isFile()
          ? "file"
          : "special",
    size: info.size,
    mtimeMs: info.mtimeMs,
    ctimeMs: info.ctimeMs,
    inode: info.ino,
    device: info.dev,
  };
}

function exists(path: string): boolean {
  try {
    lstatSync(path);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}

function fields(value: unknown, names: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Original metadata must be a JSON object");
  const record = value as Record<string, unknown>;
  return Object.fromEntries(
    names
      .filter((name) => Object.hasOwn(record, name))
      .map((name) => [name, record[name]]),
  );
}

function originalJson(root: string, relative: string, names: string[]) {
  if (!exists(join(root, relative))) return undefined;
  try {
    return {
      ref: relative,
      metadata: fields(JSON.parse(readHistoryFile(root, relative)), names),
    };
  } catch {
    return {
      ref: relative,
      status: "unknown",
      reason:
        "Original JSON metadata is unreadable or not a read-only regular document",
    };
  }
}

function invocationRefs(root: string) {
  if (!exists(join(root, "invocations")))
    return { records: [], truncated: false };
  const records: object[] = [];
  try {
    readonly(join(root, "invocations"), true);
    const directory = opendirSync(join(root, "invocations"));
    let truncated = false;
    try {
      let entry;
      let seen = 0;
      while ((entry = directory.readSync()) !== null) {
        if (seen++ === LIMIT) {
          truncated = true;
          break;
        }
        if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
        const base = `invocations/${entry.name}`;
        readonly(join(root, base), true);
        const refs = [
          "claimed.json",
          "started.json",
          "retired.json",
          "completion.json",
          "cancel.json",
        ]
          .filter((name) => exists(join(root, base, name)))
          .map((name) => `${base}/${name}`);
        records.push({
          token: entry.name,
          refs,
          currentState: "unknown",
          claim: originalJson(root, `${base}/claimed.json`, [
            "launch_token",
            "binding",
            "adapter",
          ]),
          start: originalJson(root, `${base}/started.json`, [
            "pid",
            "launch_token",
            "started_at",
          ]),
          completion: originalJson(root, `${base}/completion.json`, [
            "completion_ref",
          ]),
        });
      }
    } finally {
      directory.closeSync();
    }
    return { records, truncated };
  } catch {
    return {
      records,
      truncated: true,
      status: "unknown",
      reason: "Invocation references could not be read from the snapshot",
    };
  }
}

const TABLES = {
  control: [
    "study_id",
    "max_iterations",
    "max_calls",
    "max_seconds",
    "admission_deadline",
    "calls",
    "iterations",
    "elapsed_seconds",
    "stop_reason",
    "owner_epoch",
    "owner_open",
    "pool_version",
    "admission_mode",
  ],
  operations: [
    "id",
    "logical_id",
    "execute_attempt_id",
    "authorization_id",
    "request_ref",
    "backend",
    "backend_version",
    "deadline",
    "revision",
    "state",
    "submission_token",
    "job_id",
    "terminal_outcome",
    "physical_released",
    "collection_state",
    "collection_ref",
    "external_open",
    "unexpected_job_ids_json",
    "submit_forbidden",
  ],
  operation_authorities: [
    "id",
    "backends_json",
    "max_operations",
    "deadline",
    "admitted_operations",
    "blocked_reason",
  ],
  dispatch_runs: [
    "token",
    "attempt_id",
    "operation_id",
    "category",
    "action_kind",
    "state",
    "status",
    "worker_retired",
    "settled",
    "deadline",
    "input_ref",
    "context_ref",
    "completion_ref",
    "settlement_ref",
  ],
  attempts: [
    "id",
    "role",
    "iteration_id",
    "status",
    "owner_epoch",
    "output_ref",
    "failure_reason",
    "origin_kind",
  ],
  iterations: ["id", "task_id", "task_revision", "revision", "phase", "status"],
  contexts: ["attempt_id", "context_ref"],
  measurements: [
    "id",
    "iteration_id",
    "candidate_id",
    "evaluation_id",
    "receipt_ref",
    "plan_attempt_id",
    "execute_attempt_id",
  ],
  publications: ["id", "attempt_id", "payload_ref"],
  migration_legacy_records: [
    "import_id",
    "kind",
    "legacy_id",
    "source_ref",
    "native_status",
  ],
} as const;

function sqliteHistory(root: string) {
  const names = [
    "runtime.sqlite3",
    "runtime.sqlite3-wal",
    "runtime.sqlite3-shm",
  ];
  for (const name of names) {
    if (!exists(join(root, name)))
      return {
        status: "unknown",
        reason: `Incomplete SQLite snapshot: ${name} missing. ${REQUIREMENT}`,
      };
    try {
      readonly(join(root, name), false);
    } catch {
      return {
        status: "unknown",
        reason: `SQLite snapshot files must all be read-only regular files. ${REQUIREMENT}`,
      };
    }
  }
  const before = names.map((name) => metadata(join(root, name)));
  let database: DatabaseSync | undefined;
  try {
    database = new DatabaseSync(join(root, "runtime.sqlite3"), {
      readOnly: true,
      allowExtension: false,
      enableForeignKeyConstraints: false,
    });
    database.exec("BEGIN");
    const version = database.prepare("PRAGMA user_version").get()?.user_version;
    const available = database
      .prepare(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
      )
      .all()
      .map((row) => String(row.name));
    if (version !== 8)
      return {
        status: "unknown",
        schemaVersion: version,
        tables: available,
        reason:
          "Unsupported original SQLite schema; only schema 8 metadata is interpreted",
      };
    const tables: Record<string, object> = {};
    const warnings: string[] = [];
    for (const [table, requested] of Object.entries(TABLES)) {
      if (!available.includes(table)) {
        warnings.push(`Missing original table: ${table}`);
        continue;
      }
      const columns = database
        .prepare(`PRAGMA table_info("${table}")`)
        .all()
        .map((row) => String(row.name));
      const selected = requested.filter((name) => columns.includes(name));
      const missing = requested.filter((name) => !columns.includes(name));
      if (missing.length)
        warnings.push(
          `Missing original ${table} columns: ${missing.join(", ")}`,
        );
      if (!selected.length) continue;
      const rows = database
        .prepare(
          `SELECT ${selected.map((name) => `"${name}"`).join(",")} FROM "${table}" LIMIT ${LIMIT + 1}`,
        )
        .all();
      if (table === "control" && rows.length !== 1)
        warnings.push("Original control row is missing or ambiguous");
      tables[table] = {
        rows: rows.slice(0, LIMIT),
        truncated: rows.length > LIMIT,
      };
    }
    return {
      status: warnings.length ? "unknown" : "historical-metadata",
      schemaVersion: version,
      tables,
      warnings,
      rowLimit: LIMIT,
    };
  } catch {
    return {
      status: "unknown",
      reason: `SQLite snapshot could not be opened/read safely. ${REQUIREMENT}`,
    };
  } finally {
    database?.close();
    // Cheap file metadata only: no dataset, database-content scan or hash.
    const after = names.map((name) => metadata(join(root, name)));
    if (JSON.stringify(before) !== JSON.stringify(after))
      throw new Error(
        "SQLite snapshot changed during inspection; historical state is unknown",
      );
  }
}

/** Bounded metadata/ref inventory. This never fences, resumes or retires a job. */
export function inspectHistory(root: string): object {
  let canonical: string;
  try {
    canonical = rootPath(root);
  } catch {
    return {
      status: "unknown",
      deploymentState: "unknown",
      reason: `Writable or invalid history source refused. ${REQUIREMENT}`,
      notes: NOTES,
    };
  }
  try {
    const refs = ROOT_REFS.filter((name) => exists(join(canonical, name))).map(
      (ref) => ({ ref, ...metadata(join(canonical, ref)) }),
    );
    const sqlite = exists(join(canonical, "runtime.sqlite3"));
    const original = {
      study: originalJson(canonical, "study.json", [
        "version",
        "id",
        "study_id",
      ]),
      state: originalJson(canonical, "state.json", [
        "iteration",
        "phase",
        "status",
        "active_attempt",
        "stop_reason",
        "goal_status",
        "review_ref",
        "call_count",
        "elapsed_seconds",
      ]),
      runtime: originalJson(canonical, "runtime.json", [
        "version",
        "format",
        "state",
        "database",
        "schema_version",
        "study_id",
        "import_id",
      ]),
    };
    const database = sqlite ? sqliteHistory(canonical) : undefined;
    const supportedDocuments =
      original.study &&
      "metadata" in original.study &&
      original.study.metadata?.version === 3 &&
      original.state &&
      "metadata" in original.state &&
      typeof original.state.metadata?.phase === "string" &&
      typeof original.state.metadata?.status === "string";
    return {
      status:
        database?.status ??
        (supportedDocuments ? "historical-metadata" : "unknown"),
      format: sqlite ? "legacy-sqlite" : "legacy-documents",
      deploymentState: "unknown",
      refs,
      original,
      ...(!sqlite && !supportedDocuments
        ? {
            reason:
              "Original document schema/state metadata is unrecognized or incomplete; explicit original text remains available",
          }
        : {}),
      ...(sqlite ? { sqlite: database } : {}),
      invocations: invocationRefs(canonical),
      notes: NOTES,
    };
  } catch {
    return {
      status: "unknown",
      deploymentState: "unknown",
      reason: `History snapshot could not be read consistently. ${REQUIREMENT}`,
      notes: NOTES,
    };
  }
}
