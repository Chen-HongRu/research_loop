import { BACKGROUND_CONTEXT } from "@earendil-works/chord/context";
import { getOrThrow } from "@earendil-works/pi-durable/env";
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { JobIntent, JobResult, LocalJob } from "./experiments.ts";
import { createEnvironment } from "./environment.ts";

function command(argv: string[], intent: JobIntent, sealed?: string): string[] {
  const substitutions: Record<string, string> = {
    candidate: intent.candidatePath,
    inputs: intent.config.inputs.root,
    output: sealed ?? join(intent.directory, "predictions"),
    predictions: join(
      sealed ?? join(intent.directory, "predictions"),
      "predictions.json",
    ),
    python: intent.config.python,
  };
  if (!argv.length) throw new Error("Experiment command cannot be empty");
  return argv.map((arg) =>
    arg.replace(/\{([a-z]+)\}/g, (placeholder, key: string) => {
      if (!(key in substitutions))
        throw new Error(
          `Unknown experiment command placeholder ${placeholder}`,
        );
      return substitutions[key];
    }),
  );
}

export async function executeCpuJob(intentPath: string): Promise<void> {
  const intent = JSON.parse(readFileSync(intentPath, "utf8")) as JobIntent;
  const job: LocalJob = {
    jobId: `local-pid:${process.pid}:${intent.id}`,
    pid: process.pid,
    intent: intent.id,
    startedAt: Date.now(),
  };
  console.log(JSON.stringify({ event: "cpu-job.started", jobId: job.jobId }));
  writeFileSync(
    join(intent.directory, "job.json"),
    JSON.stringify(job) + "\n",
    { flag: "wx", mode: 0o600 },
  );
  const result: JobResult = {
    jobId: job.jobId,
    intent: intent.id,
    executionComplete: false,
    predictionExitCode: null,
    scoringExitCode: null,
    scoringStdout: "",
  };
  let environment: Awaited<ReturnType<typeof createEnvironment>> | undefined;
  try {
    const remaining = () => {
      const milliseconds = intent.deadline - Date.now();
      if (milliseconds <= 0)
        throw new Error("Original physical job time authorization expired");
      return milliseconds;
    };
    remaining();
    const predictions = join(intent.directory, "predictions");
    mkdirSync(predictions, { mode: 0o700 });
    environment = await createEnvironment({
      id: `cpu-prediction:${intent.id}`,
      workspace: intent.candidatePath,
      readOnly: [intent.candidatePath, intent.config.inputs.root],
      writable: [predictions],
      python: intent.config.python,
    });
    let predictionOutput = "";
    const prediction = await environment.env.exec(
      command(intent.config.evaluation.predictCommand, intent),
      {
        timeout: remaining() / 1000,
        inheritEnv: true,
        env: { PYTHONDONTWRITEBYTECODE: "1" },
        onOutput: (text) => {
          if (predictionOutput.length < 65536)
            predictionOutput += text.slice(0, 65536 - predictionOutput.length);
        },
      },
      BACKGROUND_CONTEXT,
    );
    writeFileSync(
      join(intent.directory, "prediction-output.txt"),
      predictionOutput,
      { flag: "wx", mode: 0o600 },
    );
    if (!prediction.ok)
      throw new Error(`Prediction process failed: ${prediction.error.code}`);
    result.predictionExitCode = prediction.value.exitCode;
    if (result.predictionExitCode !== 0)
      throw new Error("Prediction process exited unsuccessfully");
    const sourcePrediction = join(predictions, "predictions.json");
    // The candidate controls this pathname, including symlinks and special files.
    // Open it inside the original OS boundary: the official binary reader uses
    // nonblocking O_NOFOLLOW and refuses non-regular files before returning.
    const reader = getOrThrow(
      await environment.env.openBinaryReader(
        sourcePrediction,
        { noFollow: true },
        BACKGROUND_CONTEXT,
      ),
    );
    const sealed = join(intent.directory, "sealed");
    mkdirSync(sealed, { mode: 0o700 });
    try {
      const info = getOrThrow(await reader.info(BACKGROUND_CONTEXT));
      const limit = 1_048_576;
      if (info.kind !== "file" || info.size > limit)
        throw new Error(
          "Prediction output must be a regular JSON file no larger than 1 MiB",
        );
      const bytes = getOrThrow(
        await reader.read(0, limit + 1, BACKGROUND_CONTEXT),
      );
      if (bytes.length > limit)
        throw new Error("Prediction output exceeds the 1 MiB limit");
      const temporary = join(sealed, `predictions-${process.pid}.tmp`);
      const destination = join(sealed, "predictions.json");
      writeFileSync(temporary, bytes, { flag: "wx", mode: 0o400 });
      renameSync(temporary, destination);
      result.predictionArtifact = {
        source: sourcePrediction,
        sealed: destination,
        bytes: bytes.length,
        sourceSize: info.size,
        sourceMtimeMs: info.mtimeMs,
      };
    } finally {
      await reader.close(BACKGROUND_CONTEXT);
    }
    await environment.close();
    environment = undefined;
    // This is a trusted, frozen host command. It receives prediction files, never
    // imports candidate code, and reads only the captured host-owned prediction.
    const scoring = command(intent.config.evaluation.command, intent, sealed);
    const scored = spawnSync(scoring[0], scoring.slice(1), {
      cwd: intent.directory,
      encoding: "utf8",
      timeout: remaining(),
      maxBuffer: 1_048_576,
      env: { PATH: "/usr/bin:/bin", PYTHONDONTWRITEBYTECODE: "1" },
    });
    result.scoringExitCode = scored.status;
    result.scoringStdout = scored.stdout ?? "";
    writeFileSync(
      join(intent.directory, "scorer-stderr.txt"),
      scored.stderr ?? "",
      { flag: "wx", mode: 0o600 },
    );
    if (scored.error)
      throw new Error("Trusted scoring process failed or timed out");
    result.executionComplete = true;
  } catch (error) {
    result.error =
      error instanceof Error ? error.message : "Local CPU job failed";
    // Both prediction and scoring must be observed complete for a completed receipt.
  } finally {
    await environment?.close();
    const temporary = join(intent.directory, `result-${process.pid}.tmp`);
    writeFileSync(temporary, JSON.stringify(result, null, 2) + "\n", {
      flag: "wx",
      mode: 0o600,
    });
    renameSync(temporary, join(intent.directory, "result.json"));
    console.log(
      JSON.stringify({
        event: "cpu-job.finished",
        jobId: job.jobId,
        executionComplete: result.executionComplete,
      }),
    );
  }
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  try {
    await executeCpuJob(process.argv[2]);
  } catch {
    console.error(
      "Local CPU backend failed before publishing a terminal result; query the original job, do not resend",
    );
    process.exitCode = 1;
  }
}
