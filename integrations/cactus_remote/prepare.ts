import { mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { CactusRemoteBackend, type RemoteConfig } from "./backend.ts";
import type { StudyConfig } from "research-loop/contract";

const [destination, privateFile, provider, model, secondsText] =
  process.argv.slice(2);
if (!destination || !privateFile || !provider || !model || !secondsText)
  throw new Error("Usage: prepare FIXTURE REMOTE.json PROVIDER MODEL SECONDS");
const seconds = Number(secondsText);
if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 1800)
  throw new Error(
    "Supply the explicitly authorized total duration, at most 1800 seconds for this protocol",
  );
const remote = JSON.parse(readFileSync(privateFile, "utf8")) as RemoteConfig;
const backend = new CactusRemoteBackend(remote);
const root = resolve(destination);
mkdirSync(root, { recursive: false, mode: 0o700 });
const source = join(root, "source");
const inputs = join(root, "inputs");
mkdirSync(source);
mkdirSync(inputs);
writeFileSync(join(source, "params.json"), '{"learning_rate":0.001}\n', {
  flag: "wx",
});
const protocol =
  "Fixed tiny CNN: RGB [batch,3,32,32], pixels /255, Adam + BCEWithLogitsLoss, one epoch, seed 0, batch 128, 4 threads. Only learning_rate in params.json is changeable, finite [0.00001,0.1]. The trusted AutoDL runner trains; candidates cannot upload Python or change device/paths/epochs/seed/model. 11,340 train and 2,835 adaptive development rows, official MLE-bench grading ROC-AUC, greater is better. No final-test feedback. Round one measures learning_rate=0.001; round two changes it based on r0001. Compare measurements without claiming statistical robustness or general research benefit.";
writeFileSync(join(inputs, "protocol.md"), protocol + "\n", { flag: "wx" });
const config: StudyConfig = {
  version: 1,
  id: randomUUID(),
  goal: "Complete and independently review two real single-GPU Cactus tiny-CNN learning-rate investigations under the frozen protocol. Explain the development ROC-AUC evidence; improvement is not required for a valid investigation.",
  constraints: [protocol],
  model: { provider, id: model, thinking: "off" },
  authorization: {
    id: randomUUID(),
    expiresAt: Date.now() + seconds * 1000,
    slots: ["round-1", "round-2"],
    jobSeconds: seconds,
  },
  maxRounds: 2,
  minimumRounds: 2,
  execution: backend.execution,
  source: { root: source, files: ["params.json"] },
  inputs: {
    root: inputs,
    version: "mle507f92e-mirror01a3db8-public-dev20-seed0-v1",
    feedback: "development",
  },
  evaluation: {
    id: "cactus-official-dev-roc-auc",
    version: backend.evaluationVersion,
    description: protocol,
    predictCommand: [],
    command: [],
    feedback: "development",
  },
  python: realpathSync(remote.localPython),
  tasks: [
    {
      id: "baseline",
      source: "human",
      question:
        "Measure the fixed one-epoch CNN baseline with learning_rate=0.001 and identify a justified learning-rate follow-up from its actual development ROC-AUC and limitations.",
      constraints: [protocol],
      basedOn: [],
    },
  ],
};
writeFileSync(
  join(root, "study.json"),
  JSON.stringify(config, null, 2) + "\n",
  { flag: "wx", mode: 0o600 },
);
console.log(
  JSON.stringify({
    prepared: true,
    expiresAt: config.authorization.expiresAt,
    model: config.model.id,
    rounds: 2,
  }),
);
