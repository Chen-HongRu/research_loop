import { createHash, randomUUID } from "node:crypto";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { getOrThrow } from "@earendil-works/pi-durable/env";
import { spawn } from "node:child_process";
import {
  closeSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Receipt, StudyConfig } from "./contract.ts";
import { createEnvironment } from "./environment.ts";

export interface JobIntent {
  id: string;
  identity: string;
  authorization: string;
  slot: string;
  candidate: string;
  candidatePath: string;
  snapshot: { files: number; bytes: number; elapsedMs: number };
  root: string;
  directory: string;
  acceptedAt: number;
  deadline: number;
  config: StudyConfig;
}
export interface LocalJob {
  jobId: string;
  pid: number;
  intent: string;
  startedAt: number;
}
export interface JobResult {
  jobId: string;
  intent: string;
  executionComplete: boolean;
  predictionExitCode: number | null;
  scoringExitCode: number | null;
  scoringStdout: string;
  predictionArtifact?: {
    source: string;
    sealed: string;
    bytes: number;
    sourceSize: number;
    sourceMtimeMs: number;
  };
  error?: string;
}
export type PendingReceipt = {
  state: "last-confirmed-started" | "unknown";
  jobId: string;
  note: string;
};

function development(config: StudyConfig): void {
  if (
    config.inputs.feedback !== "development" ||
    config.evaluation.feedback !== "development"
  ) {
    throw new Error(
      "Final-only data/evaluation cannot be used by research tools",
    );
  }
}
function directory(root: string, config: StudyConfig, slot: string): string {
  development(config);
  if (!config.authorization.slots.includes(slot))
    throw new Error("Slot is not authorized");
  const key = createHash("sha256")
    .update(JSON.stringify([config.authorization.id, slot]))
    .digest("hex");
  return join(resolve(root), "experiments", key);
}
function identity(config: StudyConfig): string {
  return createHash("sha256")
    .update(
      JSON.stringify({
        authorization: config.authorization,
        source: config.source,
        inputs: config.inputs,
        evaluation: config.evaluation,
        python: config.python,
      }),
    )
    .digest("hex");
}
function json<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
function checkIntent(
  intent: JobIntent,
  config: StudyConfig,
  slot: string,
): void {
  if (
    intent.identity !== identity(config) ||
    intent.authorization !== config.authorization.id ||
    intent.slot !== slot
  ) {
    throw new Error(
      "Authorized slot is already bound to different input/evaluation/configuration",
    );
  }
}
function loadIntent(
  root: string,
  config: StudyConfig,
  slot: string,
): JobIntent {
  const intent = json<JobIntent>(
    join(directory(root, config, slot), "intent.json"),
  );
  checkIntent(intent, config, slot);
  return intent;
}
function loadJob(intent: JobIntent): LocalJob | undefined {
  const path = join(intent.directory, "job.json");
  if (!existsSync(path)) return undefined;
  const job = json<LocalJob>(path);
  if (
    job.intent !== intent.id ||
    !Number.isSafeInteger(job.pid) ||
    job.pid <= 0 ||
    job.jobId !== `local-pid:${job.pid}:${intent.id}`
  ) {
    throw new Error("Backend job does not belong to this submission intent");
  }
  return job;
}
async function snapshot(
  root: string,
  config: StudyConfig,
  workspace: string,
  publish: boolean,
): Promise<Pick<JobIntent, "candidate" | "candidatePath" | "snapshot">> {
  const startedAt = Date.now();
  const source = realpathSync(workspace);
  const files = [...config.source.files].sort();
  if (!files.length || new Set(files).size !== files.length)
    throw new Error("Candidate requires a nonempty unique source allowlist");
  for (const name of files) {
    if (
      isAbsolute(name) ||
      name === "" ||
      name.split(/[\\/]/).some((part) => part === ".." || part === "." || !part)
    ) {
      throw new Error(
        "Candidate source file must be a relative allowlisted path",
      );
    }
  }
  const contents: { name: string; bytes: Uint8Array }[] = [];
  const isolated = await createEnvironment({
    id: `snapshot:${randomUUID()}`,
    workspace: source,
    readOnly: [source],
    writable: [],
    python: config.python,
  });
  try {
    for (const name of files) {
      // Read through the OS boundary, not trusted host readFile after a racy
      // symlink check. Only bounded allowlisted code is read; never input data.
      const reader = getOrThrow(
        await isolated.env.openBinaryReader(
          name,
          { noFollow: true },
          BACKGROUND_CONTEXT,
        ),
      );
      try {
        const info = getOrThrow(await reader.info(BACKGROUND_CONTEXT));
        if (info.kind !== "file" || info.size > 1_048_576)
          throw new Error("Candidate source must be a small regular code file");
        const bytes = getOrThrow(
          await reader.read(0, 1_048_577, BACKGROUND_CONTEXT),
        );
        if (bytes.length > 1_048_576)
          throw new Error(
            "Candidate source exceeds the small-code snapshot limit",
          );
        contents.push({ name, bytes });
      } finally {
        await reader.close(BACKGROUND_CONTEXT);
      }
    }
  } finally {
    await isolated.close();
  }
  const hash = createHash("sha256").update(
    JSON.stringify([config.python, config.evaluation.predictCommand, files]),
  );
  for (const file of contents)
    hash
      .update(JSON.stringify([file.name, file.bytes.length]))
      .update(file.bytes);
  const candidate = hash.digest("hex");
  const base = join(resolve(root), "candidates");
  const candidatePath = join(base, candidate);
  const diagnostics = () => ({
    files: files.length,
    bytes: contents.reduce((sum, file) => sum + file.bytes.length, 0),
    elapsedMs: Date.now() - startedAt,
  });
  if (!publish) return { candidate, candidatePath, snapshot: diagnostics() };
  mkdirSync(base, { recursive: true, mode: 0o700 });
  if (!existsSync(candidatePath)) {
    const temporary = mkdtempSync(join(base, ".snapshot-"));
    try {
      for (const file of contents) {
        const target = join(temporary, file.name);
        mkdirSync(dirname(target), { recursive: true });
        writeFileSync(target, file.bytes, { flag: "wx", mode: 0o444 });
      }
      try {
        renameSync(temporary, candidatePath);
      } catch (error) {
        if (!existsSync(candidatePath)) throw error;
      }
    } finally {
      rmSync(temporary, { recursive: true, force: true });
    }
  }
  return { candidate, candidatePath, snapshot: diagnostics() };
}

function authorized(root: string, config: StudyConfig): void {
  if (existsSync(join(root, "STOP")))
    throw new Error("STOP forbids a new experiment submission");
  if (!Number.isFinite(config.authorization.expiresAt))
    throw new Error(
      "Original experiment authorization requires a finite expiry",
    );
  if (Date.now() >= config.authorization.expiresAt)
    throw new Error("Original experiment authorization expired");
  if (
    !(
      config.authorization.jobSeconds > 0 &&
      Number.isFinite(config.authorization.jobSeconds)
    )
  )
    throw new Error("Invalid job time authorization");
}

function acknowledge(intent: JobIntent, job: LocalJob): void {
  const path = join(intent.directory, "ack.json");
  if (existsSync(path)) {
    const ack = json<{ jobId: string; intent: string }>(path);
    if (ack.jobId !== job.jobId || ack.intent !== intent.id)
      throw new Error("Submission acknowledgement mismatch");
    return;
  }
  try {
    writeFileSync(
      path,
      JSON.stringify({ jobId: job.jobId, intent: intent.id }),
      { flag: "wx", mode: 0o600 },
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    acknowledge(intent, job);
  }
}

export function querySubmission(
  root: string,
  config: StudyConfig,
  slot: string,
) {
  const dir = directory(root, config, slot);
  if (!existsSync(join(dir, "intent.json")))
    return { state: "not-submitted", jobId: null };
  const intent = loadIntent(root, config, slot);
  const job = loadJob(intent);
  if (!job)
    return {
      state: "unknown",
      jobId: null,
      note: "Submission intent exists; do not resend. Inspect the original local backend.",
    };
  if (!existsSync(join(dir, "result.json"))) return pending(intent, job);
  readResult(intent, job);
  return { state: "completed", jobId: job.jobId };
}

function pending(intent: JobIntent, job: LocalJob): PendingReceipt {
  return {
    state: Date.now() >= intent.deadline ? "unknown" : "last-confirmed-started",
    jobId: job.jobId,
    note: `Only the original backend start is confirmed. No terminal result or current process identity is confirmed; inspect original PID ${job.pid}. Do not resend this slot.`,
  };
}

function acknowledgedJob(intent: JobIntent): LocalJob {
  const job = loadJob(intent);
  if (!job || !existsSync(join(intent.directory, "ack.json"))) {
    throw new Error(
      "UNKNOWN_SUBMISSION: do not resend this authorization slot; query and bind the original backend job ID, then collect",
    );
  }
  acknowledge(intent, job);
  return job;
}

function readResult(intent: JobIntent, job: LocalJob): JobResult {
  const resultPath = join(intent.directory, "result.json");
  const result = json<JobResult>(resultPath);
  if (result.intent !== intent.id || result.jobId !== job.jobId)
    throw new Error("Backend result identity mismatch");
  if (typeof result.executionComplete !== "boolean")
    throw new Error("Backend completion state is invalid");
  if (result.executionComplete) {
    const artifact = result.predictionArtifact;
    if (
      !artifact ||
      artifact.source !==
        join(intent.directory, "predictions", "predictions.json") ||
      artifact.sealed !==
        join(intent.directory, "sealed", "predictions.json") ||
      !Number.isSafeInteger(artifact.bytes) ||
      artifact.bytes < 0 ||
      artifact.bytes > 1_048_576
    ) {
      throw new Error(
        "Backend result does not reference this job's sealed prediction",
      );
    }
  }
  return result;
}

function canonicalReceipt(
  intent: JobIntent,
  config: StudyConfig,
  slot: string,
  job: LocalJob,
): Receipt | undefined {
  const resultPath = join(intent.directory, "result.json");
  if (!existsSync(resultPath)) return undefined;
  const result = readResult(intent, job);
  let valid =
    result.executionComplete &&
    result.predictionExitCode === 0 &&
    result.scoringExitCode === 0;
  let metrics: Record<string, number> = {};
  let observations: string[] = [];
  if (valid) {
    try {
      const report = JSON.parse(result.scoringStdout);
      if (
        !report ||
        typeof report.metrics !== "object" ||
        Array.isArray(report.metrics) ||
        report.metrics === null ||
        Object.values(report.metrics).some(
          (value) => typeof value !== "number" || !Number.isFinite(value),
        ) ||
        !Array.isArray(report.observations) ||
        report.observations.some(
          (value: unknown) => typeof value !== "string",
        ) ||
        (report.valid !== undefined && typeof report.valid !== "boolean")
      )
        throw new Error("Invalid scorer output");
      valid = report.valid !== false;
      if (valid) metrics = report.metrics;
      observations = report.observations;
    } catch {
      valid = false;
      observations = [
        "Trusted scorer output did not satisfy the receipt schema",
      ];
    }
  } else
    observations = [
      result.error ??
        "Prediction or scoring process did not complete successfully",
    ];
  return {
    id: `receipt:${intent.id}`,
    authorization: intent.authorization,
    slot,
    candidate: intent.candidate,
    inputVersion: config.inputs.version,
    evaluation: config.evaluation.id,
    evaluationVersion: config.evaluation.version,
    feedback: "development",
    jobId: job.jobId,
    executionComplete: result.executionComplete,
    valid,
    metrics,
    observations,
    rawResult: resultPath,
    candidatePath: intent.candidatePath,
  };
}

function checkedReceipt(path: string, expected: Receipt): Receipt {
  const cached = json<Receipt>(path);
  for (const key of Object.keys(expected) as (keyof Receipt)[]) {
    if (key === "metrics" || key === "observations") continue;
    if (cached[key] !== expected[key])
      throw new Error(`Cached receipt identity/result mismatch: ${key}`);
  }
  const metrics = cached.metrics;
  if (
    !metrics ||
    typeof metrics !== "object" ||
    Array.isArray(metrics) ||
    Object.values(metrics).some(
      (value) => typeof value !== "number" || !Number.isFinite(value),
    ) ||
    JSON.stringify(Object.entries(metrics).sort()) !==
      JSON.stringify(Object.entries(expected.metrics).sort()) ||
    JSON.stringify(cached.observations) !==
      JSON.stringify(expected.observations)
  ) {
    throw new Error(
      "Cached receipt metrics/observations mismatch with trusted backend result",
    );
  }
  // Return only canonical fields, never extra properties from a misplaced cache.
  return expected;
}

export async function collectSubmission(
  root: string,
  config: StudyConfig,
  slot: string,
): Promise<Receipt | PendingReceipt> {
  const intent = loadIntent(root, config, slot);
  const job = acknowledgedJob(intent);
  const path = join(intent.directory, "receipt.json");
  const receipt = canonicalReceipt(intent, config, slot, job);
  if (!receipt) {
    if (existsSync(path))
      throw new Error("Cached receipt has no original backend result");
    return pending(intent, job);
  }
  if (existsSync(path)) return checkedReceipt(path, receipt);
  try {
    writeFileSync(path, JSON.stringify(receipt, null, 2) + "\n", {
      flag: "wx",
      mode: 0o600,
    });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  return checkedReceipt(path, receipt);
}

export async function bindSubmission(
  root: string,
  config: StudyConfig,
  slot: string,
  jobId: string,
): Promise<Receipt | PendingReceipt> {
  const intent = loadIntent(root, config, slot);
  const job = loadJob(intent);
  if (!job || job.jobId !== jobId)
    throw new Error(
      "Job ID is not the original backend job for this authorization slot",
    );
  acknowledge(intent, job);
  return collectSubmission(root, config, slot);
}

export function readReceipts(root: string, config: StudyConfig): Receipt[] {
  development(config);
  return config.authorization.slots.flatMap((slot) => {
    const dir = directory(root, config, slot);
    const path = join(dir, "receipt.json");
    if (!existsSync(path)) return [];
    const intent = loadIntent(root, config, slot);
    const job = acknowledgedJob(intent);
    const expected = canonicalReceipt(intent, config, slot, job);
    if (!expected)
      throw new Error("Cached receipt has no original backend result");
    return [checkedReceipt(path, expected)];
  });
}

export async function runExperiment(
  root: string,
  config: StudyConfig,
  workspace: string,
  slot: string,
): Promise<Receipt> {
  const dir = directory(root, config, slot);
  const intentPath = join(dir, "intent.json");
  let intent: JobIntent;
  if (existsSync(intentPath)) {
    intent = loadIntent(root, config, slot);
    const frozen = await snapshot(root, config, workspace, false);
    if (intent.candidate !== frozen.candidate)
      throw new Error(
        "Authorized slot is already bound to a different candidate",
      );
    // An existing intent may only collect, including after STOP/expiry. It never sends.
  } else {
    authorized(root, config);
    const frozen = await snapshot(root, config, workspace, true);
    authorized(root, config);
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const acceptedAt = Date.now();
    intent = {
      id: randomUUID(),
      identity: identity(config),
      authorization: config.authorization.id,
      slot,
      ...frozen,
      root: resolve(root),
      directory: dir,
      acceptedAt,
      deadline: Math.min(
        config.authorization.expiresAt,
        acceptedAt + config.authorization.jobSeconds * 1000,
      ),
      config,
    };
    try {
      writeFileSync(intentPath, JSON.stringify(intent, null, 2) + "\n", {
        flag: "wx",
        mode: 0o600,
      });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      return runExperiment(root, config, workspace, slot);
    }
    authorized(root, config);
    const log = openSync(join(dir, "job.log"), "a", 0o600);
    try {
      const child = spawn(
        process.execPath,
        [
          "--experimental-strip-types",
          fileURLToPath(new URL("./cpu-job.ts", import.meta.url)),
          intentPath,
        ],
        {
          detached: true,
          stdio: ["ignore", log, log],
          env: {
            PATH: process.env.PATH ?? "/usr/bin:/bin",
            TMPDIR: process.env.TMPDIR ?? "/tmp",
          },
        },
      );
      // A spawn error leaves the original intent unknown; it never authorizes a retry.
      child.on("error", () => {});
      child.unref();
    } finally {
      closeSync(log);
    }
    const waitUntil = Date.now() + 5000;
    let job = loadJob(intent);
    while (!job && Date.now() < waitUntil) {
      await new Promise((resolve) => setTimeout(resolve, 25));
      job = loadJob(intent);
    }
    if (!job)
      throw new Error(
        "UNKNOWN_SUBMISSION: local job acknowledgement unavailable; query the original intent, never resend",
      );
    if (process.env.RL_CPU_FAULT === "ack")
      process.kill(process.pid, "SIGKILL");
    acknowledge(intent, job);
  }
  const collectUntil = Math.max(Date.now(), intent.deadline) + 5000;
  while (true) {
    const receipt = await collectSubmission(root, config, slot);
    if (!("state" in receipt)) return receipt;
    if (Date.now() >= collectUntil)
      throw new Error(
        `Job ${receipt.jobId} is still running or its terminal result is unknown; query/collect only, do not resend`,
      );
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}
