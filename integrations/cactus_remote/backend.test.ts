import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import type { StudyConfig } from "research-loop/contract";
import {
  CACTUS_INPUT_VERSION,
  CactusRemoteBackend,
  type RemoteConfig,
} from "./backend.ts";

const trainer = `import argparse,os
from pathlib import Path
p=argparse.ArgumentParser()
for name in ['data','output','lr','epochs','model','seed','batch-size','threads','prediction-splits','device']:
 p.add_argument('--'+name,required=True)
a=p.parse_args()
with (Path(a.data)/'starts').open('a') as f:f.write('started\\n')
assert a.epochs=='1' and a.seed=='0' and a.batch_size=='128' and a.threads=='4'
assert a.model=='tiny-cnn'
assert a.device=='cuda:0' and a.prediction_splits=='dev'
assert os.environ['CUDA_VISIBLE_DEVICES']=='GPU-synthetic'
o=Path(a.output);o.mkdir()
rows='id,has_cactus\\na.png,0.2\\nb.png,0.8\\n'
if float(a.lr)==.02:rows='id,has_cactus\\nwrong.png,0.2\\nb.png,0.8\\n'
if float(a.lr)==.03:rows='id,has_cactus\\na.png,nan\\nb.png,0.8\\n'
if float(a.lr)==.04:raise SystemExit(2)
(o/'dev-predictions.csv').write_text(rows)
(o/'untrusted-score.json').write_text('{"roc_auc":999}')
`;
const evaluator = `import argparse,csv,json,math
from pathlib import Path
import time
p=argparse.ArgumentParser();p.add_argument('--controller');p.add_argument('--submission');p.add_argument('--split');a=p.parse_args()
assert a.split=='dev'
with (Path(a.controller)/'grades').open('a') as f:f.write('graded\\n')
if (Path(a.controller)/'delay').exists():time.sleep(.3)
labels={r['id']:int(r['has_cactus']) for r in csv.DictReader((Path(a.controller)/'dev-labels.csv').open())}
rows=list(csv.DictReader(Path(a.submission).open()))
assert len(rows)==len(labels) and {r['id'] for r in rows}==set(labels)
scores={r['id']:float(r['has_cactus']) for r in rows}
assert len(scores)==len(rows) and all(math.isfinite(x) and 0<=x<=1 for x in scores.values())
positive=[scores[k] for k,y in labels.items() if y==1];negative=[scores[k] for k,y in labels.items() if y==0]
auc=sum((x>y)+.5*(x==y) for x in positive for y in negative)/(len(positive)*len(negative))
print(json.dumps({'split':'dev','metric':'roc_auc','score':auc}))
`;
function fixture() {
  const root = mkdtempSync(join(tmpdir(), "cactus-remote-synthetic-"));
  const remote = join(root, "remote");
  const source = join(root, "source");
  const controller = join(root, "private-controller");
  const bin = join(root, "bin");
  for (const directory of [
    remote,
    source,
    controller,
    bin,
    join(remote, "data"),
  ])
    mkdirSync(directory);
  const python = spawnSync(
    "uv",
    [
      "run",
      "--no-project",
      "--offline",
      "python",
      "-c",
      "import sys;print(sys.executable)",
    ],
    {
      encoding: "utf8",
      env: { ...process.env, UV_CACHE_DIR: "/private/tmp/rl96-uv-cache" },
    },
  ).stdout.trim();
  assert.ok(python.startsWith("/"));
  writeFileSync(join(remote, "train.py"), trainer);
  writeFileSync(
    join(remote, "data", "metadata.json"),
    JSON.stringify({
      mlebench_commit: "507f92e1138bb6e40dac5c6ee7a6758e6424bf97",
      source:
        "https://huggingface.co/datasets/TIGER-Lab/mle-bench/resolve/01a3db85f9245ba64d87ed2a1170d486ee147e1f/data/aerial-cactus-identification/prepared",
      official_split: { test_size: 0.19, random_state: 0 },
      dev_split: { test_size: 0.2, random_state: 0, stratify: null },
      counts: { train: 11340, dev: 2835, test: 3325 },
    }),
  );
  copyFileSync(
    fileURLToPath(new URL("./remote_runner.py", import.meta.url)),
    join(remote, "remote_runner.py"),
  );
  writeFileSync(
    join(controller, "dev-labels.csv"),
    "id,has_cactus\na.png,0\nb.png,1\n",
  );
  writeFileSync(join(root, "evaluate.py"), evaluator);
  writeFileSync(join(controller, "utils.py"), "# synthetic trusted helper\n");
  writeFileSync(join(controller, "grade.py"), "# synthetic trusted grade\n");
  writeFileSync(join(source, "params.json"), '{"learning_rate":0.001}\n');
  const privateConfig: RemoteConfig = {
    version: 1,
    sshHost: "synthetic.invalid",
    remoteRoot: remote,
    remotePython: python,
    remoteData: join(remote, "data"),
    remoteTrainer: join(remote, "train.py"),
    trainerVersion:
      "sha256:" + createHash("sha256").update(trainer).digest("hex"),
    gpu: "GPU-synthetic",
    localController: controller,
    localEvaluator: join(root, "evaluate.py"),
    localPython: python,
  };
  writeFileSync(
    join(remote, "deployment.json"),
    JSON.stringify({
      version: 1,
      remoteRoot: remote,
      remotePython: python,
      remoteData: privateConfig.remoteData,
      remoteTrainer: privateConfig.remoteTrainer,
      trainerVersion: privateConfig.trainerVersion,
      gpu: privateConfig.gpu,
    }),
  );
  const ssh = `#!${python}\nimport json,os,subprocess,sys,time\nfrom pathlib import Path\nr=json.loads(sys.stdin.read())\nroot=Path(os.environ['FAKE_CACTUS_ROOT'])\nwith (root/'calls').open('a') as f:f.write(r['action']+'\\n')\ntime.sleep(float(os.environ.get('FAKE_RPC_DELAY','0')))\np=subprocess.run([${JSON.stringify(python)},str(root/'remote_runner.py')],input=json.dumps(r),text=True,capture_output=True)\nif os.environ.get('FAKE_DROP_ACK')=='1' and r['action']=='submit':sys.exit(255)\nsys.stdout.write(p.stdout);sys.exit(p.returncode)\n`;
  writeFileSync(join(bin, "ssh"), ssh);
  chmodSync(join(bin, "ssh"), 0o755);
  const previous = {
    PATH: process.env.PATH,
    FAKE_CACTUS_ROOT: process.env.FAKE_CACTUS_ROOT,
    FAKE_DROP_ACK: process.env.FAKE_DROP_ACK,
    FAKE_RPC_DELAY: process.env.FAKE_RPC_DELAY,
  };
  process.env.PATH = `${bin}:${previous.PATH}`;
  process.env.FAKE_CACTUS_ROOT = remote;
  delete process.env.FAKE_DROP_ACK;
  const backend = new CactusRemoteBackend(privateConfig);
  const config: StudyConfig = {
    version: 1,
    id: "synthetic-study",
    goal: "Test fixed Cactus parameter execution",
    constraints: [],
    model: { provider: "not-called", id: "not-called", thinking: "off" },
    authorization: {
      id: `synthetic-${root.split("/").at(-1)}`,
      expiresAt: Date.now() + 20_000,
      slots: ["round-1", "round-2"],
      jobSeconds: 5,
    },
    maxRounds: 2,
    execution: backend.execution,
    source: { root: source, files: ["params.json"] },
    inputs: {
      root: join(remote, "data"),
      version: CACTUS_INPUT_VERSION,
      feedback: "development",
    },
    evaluation: {
      id: "synthetic-cactus-dev",
      version: backend.evaluationVersion,
      predictCommand: [],
      command: [],
      feedback: "development",
    },
    python,
    tasks: [],
  };
  return {
    root,
    remote,
    source,
    backend,
    config,
    privateConfig,
    calls: () =>
      existsSync(join(remote, "calls"))
        ? readFileSync(join(remote, "calls"), "utf8").trim().split("\n")
        : [],
    cleanup: () => {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
      rmSync(root, { recursive: true, force: true });
    },
  };
}
const native = { skip: process.platform !== "darwin", timeout: 30_000 };

test(
  "fake-SSH lost ACK then a new backend/Task only queries the one real local worker",
  native,
  async () => {
    const f = fixture();
    try {
      process.env.FAKE_DROP_ACK = "1";
      await assert.rejects(
        f.backend.run(f.root, f.config, f.source, "round-1"),
        /UNKNOWN_REMOTE_ACK/,
      );
      delete process.env.FAKE_DROP_ACK;
      const backend = new CactusRemoteBackend(f.privateConfig);
      const query = await backend.query(f.root, f.config, "round-1");
      assert.ok("job" in query && query.job?.jobRef);
      await assert.rejects(
        backend.bind(f.root, f.config, "round-1", "fake-job-ref"),
        /not the original/,
      );
      const receipt = await backend.run(f.root, f.config, f.source, "round-1");
      assert.equal(
        receipt.valid,
        true,
        readFileSync(receipt.rawResult, "utf8"),
      );
      assert.deepEqual(receipt.metrics, { roc_auc: 1 });
      assert.equal(
        receipt.jobId,
        "job" in query ? query.job?.jobRef : undefined,
      );
      assert.equal(receipt.backend?.identity, backend.execution.identity);
      assert.equal(
        readFileSync(join(f.remote, "data", "starts"), "utf8"),
        "started\n",
      );
      assert.equal(f.calls().filter((action) => action === "submit").length, 1);
      assert.equal(f.calls().filter((action) => action === "clock").length, 1);
      assert.deepEqual(backend.readReceipts(f.root, f.config), [receipt]);
      const intent = JSON.parse(
        readFileSync(join(receipt.rawResult, "..", "intent.json"), "utf8"),
      );
      assert.equal(intent.payload.expiresAt, f.config.authorization.expiresAt);
      assert.equal(
        intent.payload.remoteDeadline,
        f.config.authorization.expiresAt +
          (intent.clock.remoteNow - intent.clock.localAfter),
      );
      assert.ok(intent.clock.localBefore <= intent.clock.localAfter);
      writeFileSync(join(f.source, "params.json"), '{"learning_rate":0.01}\n');
      await assert.rejects(
        backend.run(f.root, f.config, f.source, "round-1"),
        /different candidate/,
      );
      const cache = join(receipt.rawResult, "..", "receipt.json");
      writeFileSync(
        cache,
        JSON.stringify({ ...receipt, jobId: "fake-job-ref" }),
      );
      assert.throws(
        () => backend.readReceipts(f.root, f.config),
        /Cached Cactus receipt/,
      );
      assert.equal(f.calls().filter((action) => action === "submit").length, 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "parameter/final-only/schema checks refuse before any SSH or uploaded candidate code",
  native,
  async () => {
    const f = fixture();
    try {
      const final = structuredClone(f.config);
      final.inputs.feedback = "final-only";
      await assert.rejects(
        f.backend.run(f.root, final, "/missing", "round-1"),
        /Final-only/,
      );
      assert.throws(() => f.backend.readReceipts(f.root, final), /Final-only/);
      for (const params of [
        '{"learning_rate":1e400}',
        '{"learning_rate":0.2}',
        '{"learning_rate":true}',
        '{"learning_rate":0.001,"argv":["not-allowed"]}',
      ]) {
        writeFileSync(join(f.source, "params.json"), params);
        await assert.rejects(
          f.backend.run(f.root, f.config, f.source, "round-1"),
          /learning_rate/,
        );
      }
      rmSync(join(f.source, "params.json"));
      symlinkSync(
        join(f.privateConfig.localController, "dev-labels.csv"),
        join(f.source, "params.json"),
      );
      await assert.rejects(
        f.backend.run(f.root, f.config, f.source, "round-1"),
      );
      rmSync(join(f.source, "params.json"));
      const fifo = spawnSync(f.config.python, [
        "-c",
        "import os,sys;os.mkfifo(sys.argv[1])",
        join(f.source, "params.json"),
      ]);
      assert.equal(fifo.status, 0);
      await assert.rejects(
        f.backend.run(f.root, f.config, f.source, "round-1"),
      );
      assert.deepEqual(f.calls(), []);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "a failed fixed trainer is not a completed experiment and produces no score",
  native,
  async () => {
    const f = fixture();
    try {
      writeFileSync(join(f.source, "params.json"), '{"learning_rate":0.04}\n');
      const receipt = await f.backend.run(
        f.root,
        f.config,
        f.source,
        "round-1",
      );
      assert.equal(receipt.executionComplete, false);
      assert.equal(receipt.valid, false);
      assert.deepEqual(receipt.metrics, {});
      const raw = JSON.parse(readFileSync(receipt.rawResult, "utf8"));
      assert.equal(raw.remote.state, "failed");
      assert.equal(raw.score, undefined);
      assert.equal(f.calls().filter((action) => action === "submit").length, 1);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "wrong IDs and nonfinite prediction files are rejected by local trusted scoring",
  native,
  async () => {
    const f = fixture();
    try {
      for (const [slot, lr] of [
        ["round-1", 0.02],
        ["round-2", 0.03],
      ] as const) {
        writeFileSync(
          join(f.source, "params.json"),
          JSON.stringify({ learning_rate: lr }),
        );
        const receipt = await f.backend.run(f.root, f.config, f.source, slot);
        assert.equal(receipt.executionComplete, true);
        assert.equal(receipt.valid, false);
        assert.deepEqual(receipt.metrics, {});
        assert.notEqual(
          JSON.parse(readFileSync(receipt.rawResult, "utf8")).score.exitCode,
          0,
        );
      }
      assert.equal(f.calls().filter((action) => action === "submit").length, 2);
    } finally {
      f.cleanup();
    }
  },
);

test(
  "SSH/scoring keep the event loop live, use frozen grade sources, and recover a missing receipt from raw only",
  native,
  async () => {
    const f = fixture();
    let timer: ReturnType<typeof setInterval> | undefined;
    try {
      const grading = join(
        f.root,
        "scoring-code",
        f.backend.evaluationVersion.slice(7),
        "controller",
      );
      mkdirSync(grading, { recursive: true });
      writeFileSync(join(grading, "delay"), "enabled\n");
      process.env.FAKE_RPC_DELAY = ".15";
      writeFileSync(
        f.privateConfig.localEvaluator,
        "raise RuntimeError('mutable wrapper must not execute')\n",
      );
      writeFileSync(
        join(f.privateConfig.localController, "utils.py"),
        "# changed helper must change evaluation identity\n",
      );
      let ticks = 0;
      timer = setInterval(() => ticks++, 10);
      const receipt = await f.backend.run(
        f.root,
        f.config,
        f.source,
        "round-1",
      );
      clearInterval(timer);
      timer = undefined;
      assert.ok(
        ticks >= 20,
        `RPC/scoring blocked the host event loop: only ${ticks} timer ticks`,
      );
      assert.equal(receipt.executionComplete, true);
      assert.equal(
        receipt.valid,
        true,
        readFileSync(receipt.rawResult, "utf8"),
      );
      assert.deepEqual(receipt.metrics, { roc_auc: 1 });
      const changed = new CactusRemoteBackend(f.privateConfig);
      assert.notEqual(changed.evaluationVersion, f.backend.evaluationVersion);
      await assert.rejects(
        changed.run(f.root, f.config, f.source, "round-1"),
        /evaluation differs/,
      );
      const calls = f.calls();
      const cache = join(receipt.rawResult, "..", "receipt.json");
      rmSync(cache);
      assert.deepEqual(f.backend.readReceipts(f.root, f.config), [receipt]);
      assert.equal(existsSync(cache), true);
      rmSync(cache);
      assert.deepEqual(
        await f.backend.collect(f.root, f.config, "round-1"),
        receipt,
      );
      assert.deepEqual(f.calls(), calls);
      assert.equal(readFileSync(join(grading, "grades"), "utf8"), "graded\n");
      assert.equal(
        readFileSync(join(f.remote, "data", "starts"), "utf8"),
        "started\n",
      );
    } finally {
      if (timer) clearInterval(timer);
      f.cleanup();
    }
  },
);
