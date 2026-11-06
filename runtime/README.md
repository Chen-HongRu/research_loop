# Durable research runtime

This is the active source runtime using PiDurable 1.5 and the official pi-env 1.5 daemon. Source retirement does not establish a production cutover. Legacy Studies are not executable here; see [retirement and history](../docs/runtime-retirement.md).

Requires Node.js >= 22.19, Python and [uv](https://docs.astral.sh/uv/). Native isolation currently supports macOS with `/usr/bin/sandbox-exec` only; local macOS arm64 CPU is verified. Install the locked runtime dependencies from the repository root:

```sh
npm ci
npm run build
node dist/cli.js doctor
```

The root `package-lock.json` is the canonical dependency lock. For the application archive and deployment installation, see [installation](../references/installation.md).

The [Cactus remote training adapter](../integrations/cactus_remote/README.md) describes a separate fixed-program path with [two-round remote evidence](../docs/autodl-cactus-evidence.md); the local CPU evidence is separate.

For a bounded synthetic CPU study, supply the exact provider and model you have authorized and configured in Pi. Prepare the fixture and initialize a **different**, nonexistent run directory, both outside the repository. Preparation starts the 900-second absolute authorization clock; initialize and run promptly.

```sh
uv run --no-project python benchmarks/cpu_regression/prepare.py /path/to/fixture --provider configured-provider --model configured-model --seconds 900
node dist/cli.js init /path/to/run /path/to/fixture/study.json
node dist/cli.js run /path/to/run
node dist/cli.js status /path/to/run
```

`init` validates the supplied contract and creates the run root; it makes no model call. `run` admits the configured workflow and may call the model and launch the authorized CPU jobs. The fixture allows two 30-second CPU slots and requires two reviewed rounds before the goal can be declared achieved; a Review may stop earlier. Two rounds do not promise improvement. Keep the fixture and run root available at their original paths for recovery. See the [fixture](../benchmarks/cpu_regression/README.md) and [operating contract](../docs/durable-research.md) for evaluation and authorization details.

Use one writable host per Study. Do not run `run`, `resume` or `maintain`
concurrently for the same root. Read-only status and original-job inspection do
not start a second workflow executor.

A zero exit code means the workflow completed. Inspect `result.goal`, the
independent Reviews and valid receipts to decide whether the scientific goal was
achieved; the authorized round limit alone is not success.

Inspect and recover an existing run using its original slot and job identity:

```sh
node dist/cli.js status /path/to/run
node dist/cli.js submission /path/to/run SLOT
node dist/cli.js submission /path/to/run SLOT bind JOB_ID
node dist/cli.js submission /path/to/run SLOT collect
node dist/cli.js resume /path/to/run
node dist/cli.js maintain /path/to/run
node dist/cli.js stop /path/to/run
```

The bare `submission` command queries the original intent and job; `bind` verifies the original job ID and collects, while `collect` requires its acknowledgement. Never resend an unknown submission. `resume` can create a new framework task for a terminal unsuccessful task only while the original authorization remains valid: reconcile and collect the original result first when recovering an interrupted experiment. It reviews that result without replaying Execute. `maintain` starts or waits for pending knowledge publication while admission remains authorized. `stop` records STOP; it does not assert that a physical job was cancelled. After STOP or expiry, use status and submission query/bind/collect for inspection and reconciliation, not new work. See the [recovery contract](../docs/durable-research.md#recovery-and-authorization).

See the [evidence and native test command](../docs/durable-research-evidence.md) and the sanitized [observations](observations.json) for measured results and limitations.
