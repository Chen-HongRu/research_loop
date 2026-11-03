# ProTok deployment configuration

The adapter requires a separately provisioned task repository and protected
fixed-protocol evaluator. This source release contains no deployment identity,
access instructions, resource authorization or private research history.

## Explicit operator settings

| Setting | Meaning |
| --- | --- |
| `PROTOK_PROJECT` or `--project` | Absolute path to the canonical ProTok repository |
| `PROTOK_CODEX` | Local Codex executable; defaults to `codex` on PATH |
| `PROTOK_BASE_COMMIT` | Acknowledged clean baseline commit (40 hex characters) |
| `PROTOK_BASE_FINGERPRINT` | Exact baseline identity from a trusted evaluator receipt (64 hex characters) |
| `PROTOK_BASE_VALUE` | Finite baseline metric from that same fixed-protocol receipt |

Initialization and historical import require all three baseline fields. No
historical identity or score is supplied by default. Existing initialized runs
retain their frozen goal, candidate references and receipts; this change does
not rewrite them. Configure settings before starting the process.

The supervisor and direct parent runner also enforce the shared
[initialization/readiness gate](README.md#initialization-and-readiness). A legacy
zero-round core-only Study is incomplete even if `study.json` and a ready state
exist. Preserve it for reconciliation; do not add a completion marker or delete
it to force a restart. Previously running legacy Studies are accepted using their
retained current-iteration metadata and frozen context, without requiring the
original three routes forever. Marked Studies still need valid current state.
Unknown unmarked staging is not automatically cleaned; only explicitly owned,
unpublished initialization staging can be rebuilt. No recovery here resets
research notes, attempts, budgets or published evidence.

Keep these values and site-specific SSH/resource configuration in the operator's
private environment. Do not infer a fingerprint or score from the current source,
and do not fabricate a historical receipt to make initialization pass.

## Run boundary

Before starting, provision the protected evaluator, fixed scientific
configuration, ownership record, resource lease and independent control directory
as required by the adapter. Read [the adapter contract](README.md) and
[research execution](research-execution.md). The supplied adapter is specific to
its fixed protein task; AeroArm should define its own protocol and backend.

From an initialized authorized run, read status with:

```sh
uv run python integrations/protok/run.py \
  --root /path/to/run --project /path/to/protok --status
```

Controller upgrades use [deploy_runtime.py](deploy_runtime.py) and its retained
HANDOFF/preparation records. Invoke the deployment script with the Python
environment intended for the service: its `sys.executable` is written into the
systemd start command. Reconcile any owned jobs and unknown external effects
before switching an execution runtime. Preserve STOP, prior receipts, accounting
and checkpoints. Runtime deployment and job execution are operator actions;
installing or reading this repository does not authorize them.

The [dashboard](dashboard.md) can run independently on loopback against explicitly
selected run/archive directories. Its network access controls are supplied by the
operator. Do not put access URLs, tokens or authentication files into source docs.
