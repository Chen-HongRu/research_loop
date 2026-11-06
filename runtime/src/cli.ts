#!/usr/bin/env node
import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { initialize, readConfig } from "./files.ts";
import {
  maintainKnowledge,
  runStudy,
  studyContext,
  studyStatus,
} from "./engine.ts";
import {
  bindSubmission,
  collectSubmission,
  querySubmission,
} from "./experiments.ts";
import type { StudyConfig } from "./contract.ts";
import { doctor } from "./doctor.ts";
import { inspectHistory, readHistoryFile } from "./history.ts";

const [command, suppliedRoot, ...args] = process.argv.slice(2);
try {
  if (command === "doctor") {
    console.log(JSON.stringify(doctor(), null, 2));
  } else {
    if (!suppliedRoot)
      throw new Error(
        "Usage: research-loop doctor | history ROOT [--file RELATIVE] | init ROOT CONFIG.json | run ROOT | resume ROOT | maintain ROOT | status ROOT | context ROOT [CONVERSATION_ID] [--expand] | stop ROOT | submission ROOT SLOT [bind JOB_ID | collect]",
      );
    const root = resolve(suppliedRoot);
    let result: unknown;
    switch (command) {
      case "history":
        if (args.length && (args[0] !== "--file" || args.length !== 2))
          throw new Error("history accepts only --file RELATIVE");
        result =
          args[0] === "--file"
            ? readHistoryFile(root, args[1])
            : inspectHistory(root);
        break;
      case "init": {
        if (!args[0])
          throw new Error(
            "init requires a complete authorized Study config; it makes no model call",
          );
        result = {
          root: initialize(
            root,
            JSON.parse(readFileSync(args[0], "utf8")) as StudyConfig,
          ),
        };
        break;
      }
      case "run":
      case "resume": {
        const outcome = await runStudy(root, { resume: command === "resume" });
        result = outcome;
        if (outcome.status !== "completed") process.exitCode = 1;
        break;
      }
      case "context": {
        const positional = args.filter((arg) => arg !== "--expand");
        if (
          positional.length > 1 ||
          args.filter((arg) => arg === "--expand").length > 1
        )
          throw new Error("context accepts [CONVERSATION_ID] [--expand]");
        result = await studyContext(
          root,
          positional[0] === undefined ? undefined : Number(positional[0]),
          args.includes("--expand"),
        );
        break;
      }
      case "status":
        result = await studyStatus(root);
        break;
      case "maintain":
        result = await maintainKnowledge(root);
        break;
      case "stop":
        readConfig(root);
        writeFileSync(join(root, "STOP"), "operator stop\n", { flag: "a" });
        result = {
          stopRequested: true,
          note: "Workflow cancellation does not imply physical job cancellation; inspect submissions by original identity.",
        };
        break;
      case "submission": {
        const [slot, action, jobId] = args;
        if (!slot)
          throw new Error(
            "submission requires an original authorized operation slot",
          );
        const config = readConfig(root);
        if (config.execution)
          throw new Error(
            "Use the configured experiment adapter to query, bind or collect this Study's original jobs",
          );
        result =
          action === "bind"
            ? await bindSubmission(root, config, slot, jobId)
            : action === "collect"
              ? await collectSubmission(root, config, slot)
              : querySubmission(root, config, slot);
        break;
      }
      default:
        throw new Error(
          "Unknown command. Legacy Study formats are not executable through this entry point.",
        );
    }
    if (command === "history" && args[0] === "--file")
      process.stdout.write(String(result));
    else console.log(JSON.stringify(result, null, 2));
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
