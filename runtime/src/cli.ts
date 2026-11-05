import { readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { initialize, readConfig } from "./files.ts";
import { maintainKnowledge, runStudy, studyStatus } from "./engine.ts";
import {
  bindSubmission,
  collectSubmission,
  querySubmission,
} from "./experiments.ts";
import type { StudyConfig } from "./contract.ts";

const [command, suppliedRoot, ...args] = process.argv.slice(2);
try {
  if (!suppliedRoot)
    throw new Error(
      "Usage: research-loop init ROOT CONFIG.json | run ROOT | resume ROOT | maintain ROOT | status ROOT | stop ROOT | submission ROOT SLOT [bind JOB_ID | collect]",
    );
  const root = resolve(suppliedRoot);
  let result: unknown;
  switch (command) {
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
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
