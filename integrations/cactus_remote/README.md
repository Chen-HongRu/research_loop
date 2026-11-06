# Cactus remote training adapter

This integration is being prepared for a bounded acceptance run; the real GPU and model run has not yet started. Pi, the research workflow and native coding agents run on the local macOS host. Only fixed neural-network training and prediction run on the existing AutoDL instance, inside the user's existing project directory. The authorized test uses one already idle GPU, no new rental, and at most 30 minutes total for two rounds.

## Fixed research contract

The task is Cactus from the fixed MLE-bench commit [507f92e, Lite/Low split](https://github.com/openai/mle-bench/blob/507f92e1138bb6e40dac5c6ee7a6758e6424bf97/experiments/splits/low.txt). The trusted remote tiny-CNN program, interpreter and GPU are fixed by deployment. Research may change only `learning_rate` in `params.json`, within `[1e-5, 0.1]`; training is fixed to one epoch, seed 0, batch size 128, four threads, Adam and binary cross entropy. Agent-authored code is not executed remotely: the target's namespace isolation was unavailable. The reusable runtime accepts an injected backend; task-specific implementation stays here.

Remote work uses training data and development features only. Development labels and the trusted official scorer stay on the local host. This acceptance path does not score final-test data. Adaptive development results do not establish final-test generalization.

## Preparation and execution

Prepare the fixed program, dependencies, data and deployment **before** starting the authorization clock. On the existing remote project, install `remote_runner.py`, the trusted trainer and `deployment.json` under the configured remote root. The deployment JSON fixes `version`, `remoteRoot`, `remotePython`, `remoteData`, `remoteTrainer`, `trainerVersion` and `gpu`. `trainerVersion` is the SHA-256 of the small trusted training source; it does not hash data. Pin the GPU by UUID; the runner maps that UUID through `CUDA_VISIBLE_DEVICES` to `cuda:0`. The fixed trainer uses `--prediction-splits dev`; the standalone trainer defaults to dev and test unless explicitly restricted.

Create `/path/to/private-remote.json` outside the repository with exactly these fields: `version` (1), `sshHost`, `remoteRoot`, `remotePython`, `remoteData`, `remoteTrainer`, `trainerVersion`, `gpu`, `localController`, `localEvaluator` and `localPython`. Paths and identities must match the actual deployment. Keep SSH details, host and GPU identity, API configuration and raw logs outside Git. The local controller retains `dev-labels.csv` and the upstream `utils.py` and `grade.py`; the remote receives no development labels.

From the repository root, build both the main CLI and this adapter. Once all dependencies and the remote deployment are ready, prepare a new external fixture and immediately initialize and run a different new Study root. Substitute the authorized configured provider and model:

```sh
npm ci
npm run build
node dist/cactus_remote/prepare.js /path/to/fixture /path/to/private-remote.json configured-provider configured-model 1800
node dist/cactus_remote/cli.js init /path/to/new-study /path/to/private-remote.json /path/to/fixture/study.json
node dist/cactus_remote/cli.js run /path/to/new-study /path/to/private-remote.json
node dist/cactus_remote/cli.js status /path/to/new-study /path/to/private-remote.json
```

Preparation freezes the two rounds' shared absolute 1,800-second deadline; initialization does not refresh it. The first round measures `learning_rate=0.001`; the intended second round uses the first Review to select a learning-rate change. The local trusted scorer evaluates development predictions, and an independent Review interprets each receipt. Two rounds do not promise improvement.

For the same Study and original private configuration, use `resume`, `maintain`, `status` or `stop` with the same `COMMAND ROOT REMOTE.json` arguments. Inspect and reconcile the original slot as needed:

```sh
node dist/cactus_remote/cli.js submission /path/to/new-study /path/to/private-remote.json SLOT
node dist/cactus_remote/cli.js submission /path/to/new-study /path/to/private-remote.json SLOT bind JOB_REF
node dist/cactus_remote/cli.js submission /path/to/new-study /path/to/private-remote.json SLOT collect
```

If acknowledgement is unknown, never resend. Query the original identity, bind the original job only after verification, and collect its result. A new framework task must not refresh the deadline or create another operation for that slot. The remote job enforces its original deadline independently, including after SSH disconnect. STOP does not imply physical job cancellation; reconcile remote effects on the original platform.

No real remote acceptance run or production cutover is claimed by this document. See the [fixture](../../benchmarks/cactus/README.md) and [retirement boundaries](../../docs/runtime-retirement.md).
