import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { DBOS, DBOSClient } from "@dbos-inc/dbos-sdk";
import {
  createAgentSession,
  CURRENT_SESSION_VERSION,
  DefaultResourceLoader,
  SessionManager,
  SettingsManager,
} from "@earendil-works/pi-coding-agent";
import {
  checkDeadline,
  initRun,
  PROMPT,
  readRun,
  trace,
  verify,
  type RunConfig,
} from "./common.ts";
import { createRuntime } from "./provider.ts";

const APPLICATION = "persistent-coding-spike";
const VERSION = "dbos-pi-v1";
const IDENTITY = "persistent-coding-episode";

export interface EpisodeResult {
  sessionFile: string;
  sessionId: string;
  output: string;
}

function fingerprint(config: RunConfig): string {
  return createHash("sha256")
    .update(
      JSON.stringify([
        config.id,
        config.candidate,
        realpathSync(config.workspace),
        config.provider,
        config.model,
        config.reasoning,
        config.deadline,
        PROMPT,
      ]),
    )
    .digest("hex");
}

export function dbosIdentity(root: string, config: RunConfig) {
  const key = createHash("sha256")
    .update(`${realpathSync(root)}\0${config.id}`)
    .digest("hex");
  return {
    workflowID: `persistent-coding-${key}`,
    executorID: `persistent-coding-${key}`,
    sessionFile: join(resolve(root), "dbos", "episode.jsonl"),
    fingerprint: fingerprint(config),
  };
}

function manual(reason: string, sessionFile: string): never {
  throw new Error(
    `MANUAL_RECONCILIATION_REQUIRED: ${reason}. No prompt was replayed. Inspect ${sessionFile} and the workspace/tool effects; keep this run stopped. After human reconciliation, use a new run ID and workspace for any new episode.`,
  );
}

/** Read only: never continue an existing episode or repair a truncated Pi log. */
export function reconcileDbosSession(
  root: string,
  config: RunConfig,
): EpisodeResult | null {
  const identity = dbosIdentity(root, config);
  if (!existsSync(identity.sessionFile)) return null;
  try {
    // Pi's normal loader tolerates malformed lines. A recovery decision must not
    // mistake a damaged tail for an earlier terminal assistant response.
    const raw = readFileSync(identity.sessionFile, "utf8");
    if (!raw.endsWith("\n"))
      manual("session has an incomplete tail", identity.sessionFile);
    const parsed = raw
      .trimEnd()
      .split("\n")
      .map((line) => JSON.parse(line));
    if (
      parsed[0]?.type !== "session" ||
      parsed[0]?.version !== CURRENT_SESSION_VERSION
    ) {
      manual(
        "session format differs; automatic migration is disabled during reconciliation",
        identity.sessionFile,
      );
    }
    const manager = SessionManager.open(identity.sessionFile);
    if (realpathSync(manager.getCwd()) !== realpathSync(config.workspace)) {
      manual("session workspace does not match", identity.sessionFile);
    }
    const entries = manager.getEntries();
    const binding = entries.filter(
      (e) => e.type === "custom" && e.customType === IDENTITY,
    );
    if (
      binding.length !== 1 ||
      binding[0].type !== "custom" ||
      (binding[0].data as { fingerprint?: string } | undefined)?.fingerprint !==
        identity.fingerprint
    ) {
      manual(
        "session identity does not match the frozen run",
        identity.sessionFile,
      );
    }
    const messages = manager.buildSessionContext().messages;
    const prompts = messages.filter((m) => m.role === "user");
    if (
      prompts.length !== 1 ||
      JSON.stringify(prompts[0].content) !==
        JSON.stringify([{ type: "text", text: PROMPT }])
    ) {
      manual(
        "session prompt does not match this episode",
        identity.sessionFile,
      );
    }
    const last = messages.at(-1);
    if (
      last?.role !== "assistant" ||
      last.stopReason !== "stop" ||
      last.content.some((part) => part.type === "toolCall")
    ) {
      manual(
        "episode is interrupted or has unresolved tool/model effects",
        identity.sessionFile,
      );
    }
    const output = last.content
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join("\n");
    if (!output.trim())
      manual("terminal assistant has no result", identity.sessionFile);
    return {
      sessionFile: identity.sessionFile,
      sessionId: manager.getSessionId(),
      output,
    };
  } catch (error) {
    if (
      error instanceof Error &&
      error.message.startsWith("MANUAL_RECONCILIATION_REQUIRED:")
    )
      throw error;
    return manual("session cannot be safely read", identity.sessionFile);
  }
}

function guard(root: string, config: RunConfig) {
  const current = readRun(root);
  if (fingerprint(current) !== fingerprint(config))
    throw new Error("Frozen run configuration changed");
  checkDeadline(config);
  if (existsSync(join(root, "STOP"))) throw new Error("Run stopped");
}

async function codingEpisode(
  root: string,
  config: RunConfig,
): Promise<EpisodeResult> {
  guard(root, config);
  const recovered = reconcileDbosSession(root, config);
  if (recovered) {
    trace(root, "episode_reconciled", {
      candidate: "dbos",
      sessionId: recovered.sessionId,
    });
    return recovered;
  }
  const identity = dbosIdentity(root, config);
  mkdirSync(dirname(identity.sessionFile), { recursive: true });
  try {
    // Claim the exact Pi session path before constructing a provider. Pi's public
    // open() initializes an empty file itself. A crash even before initialization
    // leaves an ambiguous existing session and therefore cannot trigger replay.
    writeFileSync(identity.sessionFile, "", { flag: "wx" });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") {
      const existing = reconcileDbosSession(root, config);
      if (existing) return existing;
      manual(
        "another episode attempt already owns this run",
        identity.sessionFile,
      );
    }
    throw error;
  }
  const manager = SessionManager.open(
    identity.sessionFile,
    undefined,
    config.workspace,
  );
  manager.appendCustomEntry(IDENTITY, { fingerprint: identity.fingerprint });
  trace(root, "episode_started", {
    candidate: "dbos",
    sessionId: manager.getSessionId(),
  });
  const runtime = await createRuntime(root, config);
  const model = runtime.getPhysicalModel(config.provider, config.model);
  if (!model) throw new Error("Configured model is unavailable");
  const agentDir = join(root, "dbos", "agent");
  const settingsManager = SettingsManager.inMemory({
    compaction: { enabled: false },
    retry: { enabled: false },
    cacheWarming: "off",
  });
  const resourceLoader = new DefaultResourceLoader({
    cwd: config.workspace,
    agentDir,
    settingsManager,
    noExtensions: true,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
  });
  await resourceLoader.reload();
  const { session } = await createAgentSession({
    cwd: config.workspace,
    agentDir,
    modelRuntime: runtime,
    model,
    thinkingLevel: config.reasoning,
    sessionManager: manager,
    settingsManager,
    resourceLoader,
    tools: ["read", "write", "edit", "bash"],
  });
  const unsubscribe = session.subscribe((event) => {
    if (event.type === "agent_settled")
      trace(root, "pi_settled", { candidate: "dbos", aborted: event.aborted });
    if (
      event.type === "tool_execution_start" ||
      event.type === "tool_execution_end"
    ) {
      trace(
        root,
        event.type === "tool_execution_start" ? "tool.start" : "tool.end",
        {
          candidate: "dbos",
          name: event.toolName,
          toolCallId: event.toolCallId,
          ...(event.type === "tool_execution_end"
            ? { isError: event.isError, result: event.result }
            : { arguments: event.args }),
        },
      );
      if (
        event.type === "tool_execution_end" &&
        event.toolName === "edit" &&
        process.env.RL_SPIKE_FAULT === "edit"
      ) {
        trace(root, "fault.edit", { candidate: "dbos" });
        process.kill(process.pid, "SIGKILL");
      }
    }
  });
  let stopError: Error | undefined;
  const abort = () => {
    void session.abort();
  };
  const terminate = () => {
    stopError = new Error(
      "Host terminated; preserve the interrupted Pi session for manual reconciliation",
    );
    abort();
  };
  const interrupt = () => {
    writeFileSync(join(root, "STOP"), "operator interrupt\n", { flag: "a" });
    stopError = new Error("Run stopped by operator interrupt");
    abort();
  };
  process.once("SIGTERM", terminate);
  process.once("SIGINT", interrupt);
  const cancelSignal = DBOS.stepStatus?.cancelSignal;
  cancelSignal?.addEventListener("abort", abort, { once: true });
  const timer = setInterval(() => {
    try {
      guard(root, config);
    } catch (error) {
      stopError = error instanceof Error ? error : new Error("Run stopped");
      abort();
    }
  }, 50);
  try {
    guard(root, config);
    if (cancelSignal?.aborted) throw new Error("DBOS workflow cancelled");
    await session.prompt(PROMPT, { expandPromptTemplates: false });
    await session.waitForIdle();
    trace(root, "pi_idle", {
      candidate: "dbos",
      stats: session.getSessionStats(),
    });
    if (stopError) throw stopError;
    guard(root, config);
    const result = reconcileDbosSession(root, config);
    if (!result) throw new Error("Pi did not persist its session");
    trace(root, "episode_completed", {
      candidate: "dbos",
      sessionId: result.sessionId,
    });
    if (process.env.RL_SPIKE_FAULT === "ack") {
      trace(root, "fault_ack", { candidate: "dbos" });
      process.kill(process.pid, "SIGKILL");
    }
    return result;
  } finally {
    clearInterval(timer);
    process.off("SIGTERM", terminate);
    process.off("SIGINT", interrupt);
    cancelSignal?.removeEventListener("abort", abort);
    unsubscribe();
    session.dispose();
  }
}

const episodeStep = DBOS.registerStep(codingEpisode, {
  name: "piCodingEpisode",
  retriesAllowed: false,
});
const verificationStep = DBOS.registerStep(
  async (root: string, config: RunConfig, answer: string) => {
    guard(root, config);
    if (!answer.trim()) throw new Error("Coding ended without an answer");
    const result = verify(config);
    trace(root, "verification", { candidate: "dbos", ...result });
    if (!result.passed)
      throw new Error(
        "Independent workspace tests failed; the model answer is not accepted",
      );
    return result;
  },
  { name: "verifyWorkspace", retriesAllowed: false },
);
const workflow = DBOS.registerWorkflow(
  async (root: string, config: RunConfig) => {
    const episode = await episodeStep(root, config);
    const verification = await verificationStep(root, config, episode.output);
    return { episode, verification };
  },
  { name: "persistentCoding", maxRecoveryAttempts: 3 },
);

function databaseURL(): string {
  const url = process.env.DBOS_SYSTEM_DATABASE_URL;
  if (!url)
    throw new Error(
      "Set DBOS_SYSTEM_DATABASE_URL to an authorized local PostgreSQL database",
    );
  return url;
}

export async function runDbos(root: string) {
  root = realpathSync(root);
  const config = readRun(root);
  if (config.candidate !== "dbos") throw new Error("Expected a DBOS run");
  guard(root, config);
  const identity = dbosIdentity(root, config);
  DBOS.setConfig({
    name: APPLICATION,
    applicationVersion: VERSION,
    executorID: identity.executorID,
    systemDatabaseUrl: databaseURL(),
    enableOTLP: false,
    logLevel: "error",
  });
  await DBOS.launch();
  try {
    const handle = await DBOS.startWorkflow(workflow, {
      workflowID: identity.workflowID,
      timeoutMS: Math.max(1, config.deadline - Date.now()),
    })(root, config);
    return await handle.getResult();
  } finally {
    await DBOS.shutdown();
  }
}

export async function statusDbos(root: string) {
  const config = readRun(root);
  const client = await DBOSClient.create({
    systemDatabaseUrl: databaseURL(),
    applicationName: APPLICATION,
  });
  try {
    return (
      (await client.getWorkflow(dbosIdentity(root, config).workflowID)) ?? null
    );
  } finally {
    await client.destroy();
  }
}

export async function cancelDbos(root: string) {
  const config = readRun(root);
  writeFileSync(join(root, "STOP"), "operator stop\n", { flag: "a" });
  trace(root, "stop_requested", { candidate: "dbos" });
  const client = await DBOSClient.create({
    systemDatabaseUrl: databaseURL(),
    applicationName: APPLICATION,
  });
  try {
    await client.cancelWorkflow(dbosIdentity(root, config).workflowID);
  } finally {
    await client.destroy();
  }
  return { stopped: true };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  const [command, root, provider, model, reasoning] = process.argv.slice(2);
  if (!root || !["init", "run", "status", "cancel"].includes(command)) {
    console.error(
      "Usage: node dbos.ts init|run|status|cancel ROOT [PROVIDER MODEL [off|medium]]",
    );
    process.exitCode = 2;
  } else {
    const actions = { run: runDbos, status: statusDbos, cancel: cancelDbos };
    try {
      const result =
        command === "init"
          ? initRun(root, "dbos", {
              ...(provider ? { provider, model } : {}),
              reasoning: reasoning as "off" | "medium" | undefined,
            })
          : await actions[command as keyof typeof actions](root);
      console.log(JSON.stringify(result));
    } catch (error) {
      console.error(
        error instanceof Error ? error.message : "DBOS command failed",
      );
      process.exitCode = 1;
    }
  }
}
