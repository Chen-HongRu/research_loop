#!/usr/bin/env node
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { initialize, readConfig } from "research-loop/files";
import { maintainKnowledge, runStudy, studyStatus } from "research-loop/engine";
import { CactusRemoteBackend, type RemoteConfig } from "./backend.ts";
import type { StudyConfig } from "research-loop/contract";

const [command, suppliedRoot, privateFile, ...args] = process.argv.slice(2);
try {
  if (!suppliedRoot || !privateFile)
    throw new Error(
      "Usage: cactus-remote init ROOT REMOTE.json STUDY.json | run ROOT REMOTE.json | resume ROOT REMOTE.json | maintain ROOT REMOTE.json | status ROOT REMOTE.json | stop ROOT REMOTE.json | submission ROOT REMOTE.json SLOT [bind JOB_REF | collect]",
    );
  const root = resolve(suppliedRoot);
  const remote = JSON.parse(readFileSync(privateFile, "utf8")) as RemoteConfig;
  const experiments = new CactusRemoteBackend(remote);
  let result: unknown;
  if (command === "init") {
    if (!args[0])
      throw new Error("init requires the complete frozen Study config");
    const config = JSON.parse(readFileSync(args[0], "utf8")) as StudyConfig;
    if (
      JSON.stringify(config.execution) !== JSON.stringify(experiments.execution)
    )
      throw new Error(
        "Private backend does not match the Study execution identity",
      );
    result = { root: initialize(root, config) };
  } else if (command === "run" || command === "resume") {
    const outcome = await runStudy(root, {
      experiments,
      resume: command === "resume",
    });
    result = outcome;
    if (outcome.status !== "completed") process.exitCode = 1;
  } else if (command === "maintain") {
    result = await maintainKnowledge(root, { experiments });
  } else if (command === "status") {
    result = await studyStatus(root);
  } else if (command === "stop") {
    const config = readConfig(root);
    if (
      JSON.stringify(config.execution) !== JSON.stringify(experiments.execution)
    )
      throw new Error("Private backend identity changed");
    writeFileSync(join(root, "STOP"), "operator stop\n", { flag: "a" });
    result = { stopRequested: true, physicalJobCancellation: "not implied" };
  } else if (command === "submission") {
    const [slot, action, jobRef] = args;
    if (!slot)
      throw new Error("submission requires the original authorized slot");
    const config = readConfig(root);
    result =
      action === "bind"
        ? await experiments.bind(root, config, slot, jobRef)
        : action === "collect"
          ? await experiments.collect(root, config, slot)
          : await experiments.query(root, config, slot);
  } else throw new Error("Unknown Cactus remote command");
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
