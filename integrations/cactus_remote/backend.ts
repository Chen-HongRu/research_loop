import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import {
  existsSync,
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  openSync,
  readlinkSync,
  renameSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { join, posix, resolve } from "node:path";
import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { getOrThrow } from "@earendil-works/pi-durable/env";
import type {
  ExperimentBackend,
  Receipt,
  StudyConfig,
} from "research-loop/contract";
import { createEnvironment } from "research-loop/environment";

const PROTOCOL = "tiny-cnn-1epoch-dev-v1";
export const CACTUS_INPUT_VERSION =
  "mle507f92e-mirror01a3db8-public-dev20-seed0-v1";
const LIMIT = 1_048_576;
export interface RemoteConfig {
  version: 1;
  sshHost: string;
  remoteRoot: string;
  remotePython: string;
  remoteData: string;
  remoteTrainer: string;
  trainerVersion: string;
  gpu: string;
  localController: string;
  localEvaluator: string;
  localPython: string;
}
interface Payload {
  protocol: string;
  authorization: string;
  slot: string;
  candidate: string;
  params: { learning_rate: number };
  trainerVersion: string;
  inputVersion: string;
  evaluation: string;
  evaluationVersion: string;
  gpu: string;
  expiresAt: number;
  remoteDeadline: number;
  jobSeconds: number;
  executionIdentity: string;
  remoteDeploymentIdentity: string;
}
interface Intent {
  configIdentity: string;
  payload: Payload;
  candidatePath: string;
  clock: { localBefore: number; localAfter: number; remoteNow: number };
}
interface Job {
  jobRef: string;
  identity: { boot_id: string; pid: number; starttime: string; nonce: string };
  nonce: string;
}
interface RemoteState {
  ok: boolean;
  error?: string;
  operationKey: string;
  payload?: Payload;
  state: string;
  job?: Job;
  result?: {
    operationKey: string;
    jobRef: string;
    payload: Payload;
    status: string;
    exitCode: number | null;
    predictionBytes?: number;
  };
  predictionsBase64?: string;
}
export interface PendingRemote {
  state: "unknown" | "last-confirmed-started";
  jobId: string | null;
  operationId: string;
  note: string;
}
interface Raw {
  remote: RemoteState;
  evaluationVersion: string;
  score?: { exitCode: number | null; stdout: string; stderr: string };
  predictionFile?: string;
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`)
      .join(",")}}`;
  return JSON.stringify(value);
}
function hash(value: unknown): string {
  return createHash("sha256").update(stable(value)).digest("hex");
}
function json<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
function atomic(path: string, value: unknown): void {
  const temporary = `${path}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", {
    flag: "wx",
    mode: 0o600,
  });
  renameSync(temporary, path);
}
function quote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`;
}

function smallSource(path: string): Buffer {
  const fd = openSync(
    path,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const info = fstatSync(fd);
    if (!info.isFile() || info.size > LIMIT)
      throw new Error("Trusted grading source must be a small regular file");
    const bytes = readFileSync(fd);
    if (bytes.length > LIMIT)
      throw new Error("Trusted grading source exceeds size limit");
    return bytes;
  } finally {
    closeSync(fd);
  }
}

function execute(
  file: string,
  args: string[],
  options: {
    timeout: number;
    maxBuffer: number;
    cwd?: string;
    env?: NodeJS.ProcessEnv;
  },
  input?: string,
): Promise<{
  status: number | null;
  stdout: string;
  stderr: string;
  error: Error | null;
}> {
  return new Promise((resolve) => {
    const child = execFile(
      file,
      args,
      { ...options, encoding: "utf8" },
      (error, stdout, stderr) => {
        const code = (error as NodeJS.ErrnoException | null)?.code;
        resolve({
          status: error ? (typeof code === "number" ? code : null) : 0,
          stdout,
          stderr,
          error,
        });
      },
    );
    child.stdin?.end(input);
  });
}

/** Cactus only: fixed remote trainer, parameter-only candidates, local official grade. */
export class CactusRemoteBackend implements ExperimentBackend {
  readonly execution: ExperimentBackend["execution"];
  readonly evaluationVersion: string;
  private readonly config: RemoteConfig;
  private readonly grading: Record<string, Buffer>;
  private readonly remoteDeploymentIdentity: string;
  constructor(privateConfig: RemoteConfig) {
    const keys = [
      "version",
      "sshHost",
      "remoteRoot",
      "remotePython",
      "remoteData",
      "remoteTrainer",
      "trainerVersion",
      "gpu",
      "localController",
      "localEvaluator",
      "localPython",
    ];
    if (
      Object.keys(privateConfig).sort().join() !== [...keys].sort().join() ||
      privateConfig.version !== 1
    )
      throw new Error("Invalid fixed Cactus remote configuration");
    for (const key of keys.filter((key) => key !== "version") as Exclude<
      keyof RemoteConfig,
      "version"
    >[]) {
      if (
        typeof privateConfig[key] !== "string" ||
        !privateConfig[key] ||
        /[\0\r\n]/.test(privateConfig[key])
      )
        throw new Error("Invalid fixed Cactus configuration value");
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9_.@:-]*$/.test(privateConfig.sshHost))
      throw new Error("Use a fixed SSH host/alias, never SSH arguments");
    for (const key of [
      "remoteRoot",
      "remotePython",
      "remoteData",
      "remoteTrainer",
      "localController",
      "localEvaluator",
      "localPython",
    ] as const) {
      if (
        !posix.isAbsolute(privateConfig[key]) ||
        posix.normalize(privateConfig[key]) !== privateConfig[key] ||
        privateConfig[key] === "/"
      )
        throw new Error("Cactus paths must be fixed absolute paths");
    }
    if (
      !/^(sha256:)?[a-f0-9]{64}$/.test(privateConfig.trainerVersion) ||
      !/^GPU-[A-Za-z0-9-]+$/.test(privateConfig.gpu)
    )
      throw new Error("Pin the trainer source SHA256 and GPU UUID");
    this.config = Object.freeze({ ...privateConfig });
    this.grading = {
      "evaluate.py": smallSource(this.config.localEvaluator),
      "utils.py": smallSource(join(this.config.localController, "utils.py")),
      "grade.py": smallSource(join(this.config.localController, "grade.py")),
    };
    this.evaluationVersion =
      "sha256:" +
      hash(
        Object.fromEntries(
          Object.entries(this.grading).map(([name, bytes]) => [
            name,
            bytes.toString("base64"),
          ]),
        ),
      );
    this.remoteDeploymentIdentity = hash({
      version: 1,
      remoteRoot: this.config.remoteRoot,
      remotePython: this.config.remotePython,
      remoteData: this.config.remoteData,
      remoteTrainer: this.config.remoteTrainer,
      trainerVersion: this.config.trainerVersion,
      gpu: this.config.gpu,
    });
    this.execution = Object.freeze({
      id: "cactus-autodl",
      version: "1",
      identity: hash({ protocol: PROTOCOL, configuration: this.config }),
    });
  }

  private check(config: StudyConfig, slot: string): void {
    if (
      config.inputs.feedback !== "development" ||
      config.evaluation.feedback !== "development"
    )
      throw new Error(
        "Final-only inputs/evaluation are forbidden for Cactus research feedback",
      );
    if (config.evaluation.version !== this.evaluationVersion)
      throw new Error(
        "Frozen Cactus evaluation differs from the three trusted grading sources",
      );
    if (config.inputs.version !== CACTUS_INPUT_VERSION)
      throw new Error(
        "Cactus public input version is not the pinned MLE/mirror/development split",
      );
    if (stable(config.execution) !== stable(this.execution))
      throw new Error(
        "Frozen execution identity does not match this Cactus deployment",
      );
    if (!config.authorization.slots.includes(slot))
      throw new Error("Unauthorized Cactus operation slot");
    if (
      config.source.files.length !== 1 ||
      config.source.files[0] !== "params.json"
    )
      throw new Error("Cactus remote candidates contain only params.json");
  }
  private operation(config: StudyConfig, slot: string): string {
    this.check(config, slot);
    return createHash("sha256")
      .update(JSON.stringify([config.authorization.id, slot]))
      .digest("hex");
  }
  private directory(root: string, config: StudyConfig, slot: string): string {
    return join(resolve(root), "remote-cactus", this.operation(config, slot));
  }
  private configIdentity(config: StudyConfig): string {
    return hash({
      execution: this.execution,
      authorization: config.authorization,
      inputs: config.inputs,
      evaluation: config.evaluation,
      source: config.source,
      python: config.python,
    });
  }
  private intent(root: string, config: StudyConfig, slot: string): Intent {
    const intent = json<Intent>(
      join(this.directory(root, config, slot), "intent.json"),
    );
    if (
      intent.configIdentity !== this.configIdentity(config) ||
      intent.payload.authorization !== config.authorization.id ||
      intent.payload.slot !== slot ||
      intent.payload.executionIdentity !== this.execution.identity ||
      intent.payload.remoteDeploymentIdentity !==
        this.remoteDeploymentIdentity ||
      intent.payload.trainerVersion !== this.config.trainerVersion ||
      intent.payload.gpu !== this.config.gpu ||
      intent.payload.inputVersion !== config.inputs.version ||
      intent.payload.evaluation !== config.evaluation.id ||
      intent.payload.evaluationVersion !== config.evaluation.version ||
      intent.payload.expiresAt !== config.authorization.expiresAt ||
      intent.payload.jobSeconds !== config.authorization.jobSeconds ||
      !intent.clock ||
      !Number.isFinite(intent.clock.remoteNow) ||
      !Number.isFinite(intent.clock.localAfter) ||
      intent.payload.remoteDeadline !==
        config.authorization.expiresAt +
          (intent.clock.remoteNow - intent.clock.localAfter)
    )
      throw new Error(
        "Cactus slot is bound to another frozen authorization/input/evaluation",
      );
    if (
      intent.payload.candidate !==
        hash({
          params: intent.payload.params,
          trainerVersion: this.config.trainerVersion,
          protocol: PROTOCOL,
        }) ||
      intent.candidatePath !==
        join(resolve(root), "candidates", intent.payload.candidate)
    )
      throw new Error("Original Cactus candidate identity/reference mismatch");
    return intent;
  }

  private scoringCode(root: string): { evaluator: string; controller: string } {
    const directory = join(
      resolve(root),
      "scoring-code",
      this.evaluationVersion.slice(7),
    );
    const controller = join(directory, "controller");
    mkdirSync(controller, { recursive: true, mode: 0o700 });
    for (const [name, bytes] of Object.entries(this.grading)) {
      const destination =
        name === "evaluate.py" ? join(directory, name) : join(controller, name);
      const temporary = `${destination}.${randomUUID()}.tmp`;
      writeFileSync(temporary, bytes, { flag: "wx", mode: 0o400 });
      renameSync(temporary, destination);
    }
    const link = join(controller, "dev-labels.csv");
    const labels = join(this.config.localController, "dev-labels.csv");
    try {
      const info = lstatSync(link);
      if (!info.isSymbolicLink() || readlinkSync(link) !== labels)
        throw new Error("Trusted grading label reference changed");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      symlinkSync(labels, link);
    }
    return { evaluator: join(directory, "evaluate.py"), controller };
  }
  private authorize(root: string, config: StudyConfig): void {
    if (existsSync(join(root, "STOP")))
      throw new Error(
        "STOP forbids a new remote submission; existing jobs require separate query/collection",
      );
    if (
      !Number.isFinite(config.authorization.expiresAt) ||
      Date.now() >= config.authorization.expiresAt
    )
      throw new Error("Original Cactus authorization expired; collection only");
    if (
      !Number.isFinite(config.authorization.jobSeconds) ||
      config.authorization.jobSeconds <= 0 ||
      config.authorization.jobSeconds > 1800
    )
      throw new Error(
        "Cactus job time authorization must be in (0,1800] seconds",
      );
  }
  private async parameters(
    workspace: string,
    python: string,
  ): Promise<{ learning_rate: number }> {
    const isolated = await createEnvironment({
      id: `cactus-params:${randomUUID()}`,
      workspace,
      readOnly: [workspace],
      writable: [],
      python,
    });
    try {
      const reader = getOrThrow(
        await isolated.env.openBinaryReader(
          "params.json",
          { noFollow: true },
          BACKGROUND_CONTEXT,
        ),
      );
      try {
        const info = getOrThrow(await reader.info(BACKGROUND_CONTEXT));
        if (info.kind !== "file" || info.size > 1024)
          throw new Error("params.json must be a small regular JSON file");
        const bytes = getOrThrow(
          await reader.read(0, 1025, BACKGROUND_CONTEXT),
        );
        if (bytes.length > 1024)
          throw new Error("params.json exceeds size limit");
        const params = JSON.parse(Buffer.from(bytes).toString("utf8"));
        if (
          !params ||
          typeof params !== "object" ||
          Array.isArray(params) ||
          Object.keys(params).join() !== "learning_rate" ||
          typeof params.learning_rate !== "number" ||
          !Number.isFinite(params.learning_rate) ||
          params.learning_rate < 1e-5 ||
          params.learning_rate > 0.1
        )
          throw new Error(
            "Only finite learning_rate in [1e-5,0.1] is accepted",
          );
        return { learning_rate: params.learning_rate };
      } finally {
        await reader.close(BACKGROUND_CONTEXT);
      }
    } finally {
      await isolated.close();
    }
  }
  private async request(request: unknown): Promise<unknown> {
    const remoteCommand = `${quote(this.config.remotePython)} ${quote(posix.join(this.config.remoteRoot, "remote_runner.py"))}`;
    const result = await execute(
      "ssh",
      [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        this.config.sshHost,
        remoteCommand,
      ],
      {
        timeout: 15_000,
        maxBuffer: 2_097_152,
      },
      JSON.stringify(request),
    );
    if (result.error || result.status !== 0)
      throw new Error(
        "UNKNOWN_REMOTE_ACK: query the original operation; never resend or replace its intent",
      );
    const response = JSON.parse(result.stdout);
    if (!response.ok)
      throw new Error(
        `Remote Cactus request refused: ${response.error ?? "unknown"}`,
      );
    return response;
  }
  private async rpc(
    action: "submit" | "query" | "collect",
    operationKey: string,
    payload: Payload,
  ): Promise<RemoteState> {
    const response = (await this.request({
      action,
      operationKey,
      payload,
    })) as RemoteState;
    this.validateResponse(response, operationKey, payload);
    return response;
  }
  private validateResponse(
    response: RemoteState,
    operationKey: string,
    payload: Payload,
  ): void {
    if (
      response.operationKey !== operationKey ||
      (response.state !== "not-submitted" &&
        stable(response.payload) !== stable(payload))
    )
      throw new Error("Remote operation payload/identity mismatch");
    if (response.job) {
      const identity = response.job.identity;
      let parsed: unknown;
      try {
        parsed = JSON.parse(response.job.jobRef);
      } catch {
        throw new Error("Invalid original remote JobRef");
      }
      if (
        !identity ||
        typeof identity.boot_id !== "string" ||
        !identity.boot_id ||
        typeof identity.starttime !== "string" ||
        !identity.starttime ||
        !Number.isSafeInteger(identity.pid) ||
        identity.pid <= 0 ||
        !/^[a-f0-9]{32}$/.test(identity.nonce) ||
        identity.nonce !== response.job.nonce ||
        stable(parsed) !== stable(identity)
      )
        throw new Error("Invalid original remote JobRef");
    }
    if (
      response.result &&
      (!response.job ||
        response.result.operationKey !== operationKey ||
        response.result.jobRef !== response.job.jobRef ||
        stable(response.result.payload) !== stable(payload) ||
        response.result.status !== response.state)
    )
      throw new Error("Remote result identity mismatch");
  }
  private acknowledge(directory: string, job: Job): void {
    const path = join(directory, "ack.json");
    if (existsSync(path)) {
      if (stable(json<Job>(path)) !== stable(job))
        throw new Error("Original remote acknowledgement mismatch");
      return;
    }
    try {
      writeFileSync(path, JSON.stringify(job) + "\n", {
        flag: "wx",
        mode: 0o600,
      });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      this.acknowledge(directory, job);
    }
  }
  private pending(key: string, response: RemoteState): PendingRemote {
    return {
      state:
        response.state === "last-confirmed-started"
          ? "last-confirmed-started"
          : "unknown",
      operationId: key,
      jobId: response.job?.jobRef ?? null,
      note: "Only recorded remote identity is known; PID does not prove liveness. Query/bind/collect this operation, never resubmit. Workflow cancellation does not prove physical job cancellation.",
    };
  }

  async query(
    root: string,
    config: StudyConfig,
    slot: string,
  ): Promise<RemoteState | PendingRemote> {
    const key = this.operation(config, slot);
    const intent = this.intent(root, config, slot);
    const response = await this.rpc("query", key, intent.payload);
    // Metadata only; query never returns local or remote scores/predictions.
    return response;
  }
  async bind(
    root: string,
    config: StudyConfig,
    slot: string,
    jobRef: string,
  ): Promise<Receipt | PendingRemote> {
    const key = this.operation(config, slot);
    const intent = this.intent(root, config, slot);
    const response = await this.rpc("query", key, intent.payload);
    if (!response.job || response.job.jobRef !== jobRef)
      throw new Error("JobRef is not the original remote job for this intent");
    this.acknowledge(this.directory(root, config, slot), response.job);
    return this.collect(root, config, slot);
  }

  private canonicalReceipt(
    root: string,
    config: StudyConfig,
    slot: string,
  ): Receipt | undefined {
    const directory = this.directory(root, config, slot);
    const rawPath = join(directory, "raw-result.json");
    if (!existsSync(rawPath)) return undefined;
    const intent = this.intent(root, config, slot);
    const key = this.operation(config, slot);
    const ack = json<Job>(join(directory, "ack.json"));
    const raw = json<Raw>(rawPath);
    if (raw.evaluationVersion !== this.evaluationVersion)
      throw new Error("Original local scoring source identity mismatch");
    this.validateResponse(raw.remote, key, intent.payload);
    if (
      !raw.remote.job ||
      stable(ack) !== stable(raw.remote.job) ||
      !raw.remote.result ||
      !["succeeded", "failed", "timed_out"].includes(raw.remote.state)
    )
      throw new Error(
        "Cached remote result is not the acknowledged terminal job",
      );
    let valid = false;
    let metrics: Record<string, number> = {};
    const observations = [
      "Remote fixed tiny CNN/Adam/BCE, one epoch, seed 0, batch 128, threads 4; development predictions only",
    ];
    if (
      raw.remote.state === "succeeded" &&
      raw.remote.result.exitCode === 0 &&
      raw.score?.exitCode === 0 &&
      raw.predictionFile === join(directory, "sealed-dev-predictions.csv")
    ) {
      try {
        const score = JSON.parse(raw.score.stdout);
        if (
          score.split !== "dev" ||
          score.metric !== "roc_auc" ||
          typeof score.score !== "number" ||
          !Number.isFinite(score.score) ||
          score.score < 0 ||
          score.score > 1
        )
          throw new Error("Invalid local ROC-AUC receipt");
        valid = true;
        metrics = { roc_auc: score.score };
        observations.push(
          "Official ROC-AUC was computed by the trusted local evaluator from sealed predictions and private development labels; remote numbers are not measurement authority",
        );
      } catch {
        observations.push(
          "Trusted local scorer returned an invalid measurement",
        );
      }
    } else
      observations.push(
        "Original remote training or trusted local prediction validation/scoring did not succeed",
      );
    return {
      id: `cactus-receipt:${key}`,
      authorization: config.authorization.id,
      slot,
      candidate: intent.payload.candidate,
      inputVersion: config.inputs.version,
      evaluation: config.evaluation.id,
      evaluationVersion: config.evaluation.version,
      feedback: "development",
      jobId: ack.jobRef,
      executionComplete:
        raw.remote.state === "succeeded" &&
        raw.remote.result.exitCode === 0 &&
        raw.score !== undefined &&
        raw.score.exitCode !== null,
      valid,
      metrics,
      observations,
      rawResult: rawPath,
      candidatePath: intent.candidatePath,
      backend: { ...this.execution, operationId: key },
    };
  }
  private checkedCache(directory: string, expected: Receipt): Receipt {
    if (!existsSync(join(directory, "receipt.json"))) {
      atomic(join(directory, "receipt.json"), expected);
      return expected;
    }
    if (
      stable(json<Receipt>(join(directory, "receipt.json"))) !==
      stable(expected)
    )
      throw new Error(
        "Cached Cactus receipt differs from original intent/job/result/feedback",
      );
    return expected;
  }
  async collect(
    root: string,
    config: StudyConfig,
    slot: string,
  ): Promise<Receipt | PendingRemote> {
    const key = this.operation(config, slot);
    const directory = this.directory(root, config, slot);
    const cached = this.canonicalReceipt(root, config, slot);
    if (cached) return this.checkedCache(directory, cached);
    const intent = this.intent(root, config, slot);
    const response = await this.rpc("collect", key, intent.payload);
    if (!response.job || !existsSync(join(directory, "ack.json")))
      throw new Error(
        "UNKNOWN_REMOTE_ACK: query and bind the original JobRef; do not resend",
      );
    this.acknowledge(directory, response.job);
    if (!response.result) return this.pending(key, response);
    const raw: Raw = {
      remote: { ...response, predictionsBase64: undefined },
      evaluationVersion: this.evaluationVersion,
    };
    if (response.state === "succeeded") {
      const encoded = response.predictionsBase64;
      if (
        typeof encoded !== "string" ||
        encoded.length > Math.ceil(LIMIT / 3) * 4 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(
          encoded,
        )
      )
        throw new Error("Remote prediction is not a bounded sealed artifact");
      const bytes = Buffer.from(encoded, "base64");
      if (
        bytes.length > LIMIT ||
        bytes.length !== response.result.predictionBytes ||
        bytes.toString("base64") !== encoded
      )
        throw new Error("Remote prediction identity/size mismatch");
      const predictionFile = join(directory, "sealed-dev-predictions.csv");
      const temporary = `${predictionFile}.${randomUUID()}.tmp`;
      writeFileSync(temporary, bytes, { flag: "wx", mode: 0o400 });
      renameSync(temporary, predictionFile);
      raw.predictionFile = predictionFile;
      const code = this.scoringCode(root);
      const score = await execute(
        this.config.localPython,
        [
          code.evaluator,
          "--controller",
          code.controller,
          "--submission",
          predictionFile,
          "--split",
          "dev",
        ],
        {
          cwd: directory,
          timeout: 30_000,
          maxBuffer: LIMIT,
          env: { PATH: "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1" },
        },
      );
      raw.score = {
        exitCode: score.status,
        stdout: score.stdout ?? "",
        stderr: score.stderr ?? "",
      };
    }
    atomic(join(directory, "raw-result.json"), raw);
    const receipt = this.canonicalReceipt(root, config, slot);
    if (!receipt) throw new Error("No original remote result was collected");
    atomic(join(directory, "receipt.json"), receipt);
    return receipt;
  }
  readReceipts(root: string, config: StudyConfig): Receipt[] {
    return config.authorization.slots.flatMap((slot) => {
      const directory = this.directory(root, config, slot);
      if (
        !existsSync(join(directory, "receipt.json")) &&
        !existsSync(join(directory, "raw-result.json"))
      )
        return [];
      const expected = this.canonicalReceipt(root, config, slot);
      if (!expected)
        throw new Error("Cached Cactus receipt has no original result");
      return [this.checkedCache(directory, expected)];
    });
  }

  async run(
    root: string,
    config: StudyConfig,
    workspace: string,
    slot: string,
  ): Promise<Receipt> {
    const key = this.operation(config, slot);
    const directory = this.directory(root, config, slot);
    const intentPath = join(directory, "intent.json");
    if (!existsSync(intentPath)) this.authorize(root, config);
    const params = await this.parameters(workspace, config.python);
    const candidate = hash({
      params,
      trainerVersion: this.config.trainerVersion,
      protocol: PROTOCOL,
    });
    let intent: Intent;
    let response: RemoteState;
    if (existsSync(intentPath)) {
      intent = this.intent(root, config, slot);
      if (intent.payload.candidate !== candidate)
        throw new Error(
          "Cactus slot is already bound to different candidate parameters",
        );
      response = await this.rpc("query", key, intent.payload);
    } else {
      this.authorize(root, config);
      const localBefore = Date.now();
      const clock = (await this.request({ action: "clock" })) as {
        nowMs: number;
      };
      const localAfter = Date.now();
      if (!Number.isFinite(clock.nowMs) || localAfter < localBefore)
        throw new Error(
          "Remote clock sample cannot bind the original deadline",
        );
      const remoteDeadline =
        config.authorization.expiresAt + (clock.nowMs - localAfter);
      this.authorize(root, config);
      const candidatePath = join(resolve(root), "candidates", candidate);
      mkdirSync(candidatePath, { recursive: true, mode: 0o700 });
      const path = join(candidatePath, "params.json");
      if (!existsSync(path))
        writeFileSync(path, JSON.stringify(params) + "\n", {
          flag: "wx",
          mode: 0o444,
        });
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      intent = {
        configIdentity: this.configIdentity(config),
        candidatePath,
        clock: { localBefore, localAfter, remoteNow: clock.nowMs },
        payload: {
          protocol: PROTOCOL,
          authorization: config.authorization.id,
          slot,
          candidate,
          params,
          trainerVersion: this.config.trainerVersion,
          inputVersion: config.inputs.version,
          evaluation: config.evaluation.id,
          evaluationVersion: config.evaluation.version,
          gpu: this.config.gpu,
          expiresAt: config.authorization.expiresAt,
          remoteDeadline,
          jobSeconds: config.authorization.jobSeconds,
          executionIdentity: this.execution.identity,
          remoteDeploymentIdentity: this.remoteDeploymentIdentity,
        },
      };
      try {
        writeFileSync(intentPath, JSON.stringify(intent, null, 2) + "\n", {
          flag: "wx",
          mode: 0o600,
        });
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        return this.run(root, config, workspace, slot);
      }
      this.authorize(root, config);
      response = await this.rpc("submit", key, intent.payload);
    }
    if (!response.job)
      throw new Error(
        "UNKNOWN_REMOTE_SUBMISSION: original claim has no JobRef; query/bind/collect only, never resend",
      );
    this.acknowledge(directory, response.job);
    const until = Math.max(Date.now(), config.authorization.expiresAt) + 5000;
    while (true) {
      const receipt = await this.collect(root, config, slot);
      if (!("state" in receipt)) return receipt;
      if (existsSync(join(root, "STOP")))
        throw new Error(
          "Workflow STOP observed; remote job cancellation is not confirmed. Query/collect its original JobRef",
        );
      if (Date.now() >= until || receipt.state === "unknown")
        throw new Error(
          "Remote job terminal state is unknown; query/bind/collect only, never resubmit",
        );
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
}
