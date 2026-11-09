# SSH process backend

Use this backend when an operator has installed an owned-process JSON launcher
on a remote machine. The controller stays local; no remote Codex is needed.
Keep real host aliases, deployment paths and goals in private files outside Git.
The following neutral resource JSON prepares the tools while disabling starts:

```json
{
  "adapter": "ssh-process",
  "host": "example.invalid",
  "remote_root": "/path/to/project",
  "remote_python": "/path/to/project/.venv/bin/python",
  "remote_launcher": "/path/to/project/scripts/process_launcher.py",
  "ledger_path": "private-job-registry.json",
  "allow_start": false,
  "gpu": null,
  "cpu_smoke": false
}
```

Pass its path to `research-loop init RUN --project PROJECT --goal GOAL --resource FILE`,
then run `research-loop inspect RUN`. Initialization and inspection only validate
local configuration/registry metadata. They perform no SSH, model call, process
start or registry write. Inspection lists the five registered tool schemas.
The existing native lead-only tool check protects calls; native shell networking
remains disabled.

The operator must authorize execution separately. An enabled configuration has
`allow_start: true` and exactly one of explicit numeric `gpu` (for example `"2"`)
or `cpu_smoke: true` with `gpu: null`. Device settings are frozen at initialization
and injected by the host; `ssh_start` exposes no device override to the lead.
Do not enable GPU starts before agreeing the actual GPU allocation and run budget.
This adapter does not impose a job time limit or implement GPU-second accounting.
Changing the source JSON after initialization does not change the frozen run.

## Remote launcher protocol

The host invokes the configured Python and launcher with `_remote`, using SSH
BatchMode and a 10-second connection timeout. It passes one JSON request on stdin
and expects one JSON object on stdout, with a nonzero exit for request failure.
The host's 20-second request timeout bounds transport waiting, not job lifetime.
Commands are argv lists; they are never interpolated into a remote shell command.
The only shell quoting is for the configured Python/launcher invocation itself.

Every request includes `action` and `project` (the configured `remote_root`).
Start also includes a host-generated `controller_token`, reserved durably before
dispatch. The remote launcher stores and echoes it in lifecycle receipts and
requires the same token on status, stop and artifact requests. A pre-existing
same-named job with a different token must never be controlled by this run.

| Action | Extra request fields | Response |
| --- | --- | --- |
| `prepare` | none | Read-only preflight object |
| `start` | `job_id`, `argv`, `config_paths`, frozen `gpu` and `cpu_smoke` | Original job receipt |
| `status` | `job_id` | Original job receipt; bounded output tail if available |
| `stop` | `job_id` | Owned-process cancellation/stop receipt |
| `read_artifact` | `job_id`, `relative_path`, `offset`, optional `limit` | `job_id`, `offset`, `size`, `content_base64` |

Lifecycle receipts contain the exact `job_id` and `state`. `completed`, `failed`
and `stopped` are terminal; retain the command's `exit_code` where available.
Every other state remains unresolved in the host registry, preventing another
start and blocking publication. A failed request never implies that dispatch
did not happen. Reconcile the original ID; never retry start or replace an
uncertain job with a new ID. Remote job directories must reserve IDs exclusively,
persist terminal outcomes, and verify ownership plus Linux process start identity
before signaling. Stop must also preserve cancellation intent during startup.
The host does not perform arbitrary remote kills.

The local registry and its lock stay outside agent-writable work/draft. They bind
host, launcher, root and device settings. A missing initialized registry or changed
identity is rejected. The host records each original ID before sending start and
does not overwrite existing identities. Cleanup stops only jobs recorded for the
same run root and queries their resulting state; an unresolved receipt halts
publication. Sharing a registry serializes dispatch until prior jobs are terminal.

## Explicit artifact access

Remote commands must place results inside their owned job directory. The remote
launcher must reject absolute/traversing paths, symlinks and files outside that
directory. `read_artifact` reads at most 1 MiB per requested chunk; status output
tails should also be bounded. No artifact or dataset scans or hashes are required.

`ssh_fetch(job_id, relative_path, max_bytes=67108864)` copies one explicit recorded
job artifact in chunks to `RUN/work/artifacts/JOB_ID/RELATIVE_PATH`. It verifies
chunk identity/offset/size, rejects files above the requested byte ceiling, and
opens local path components without following symlinks. Existing destinations are
never overwritten. Fetch stable finished artifacts; concurrent changes can fail
the size checks, and same-size content changes are not detected. A failed transfer
may leave a partial destination that the operator must inspect before any manual
removal. Local analysts can then read the file through ordinary filesystem tools.

The backend provides process lifecycle and explicit file access. It provides no
automatic artifact discovery, scientific evaluation, background event-to-agent
notification, scheduler reservation or billing. SSH availability and the installed
remote launcher's implementation remain deployment requirements. Slurm accounting
views do not establish GPU utilization or budget usage for this backend.
The dashboard and accounting readers explicitly mark GPU accounting as
not applicable for this backend, with no fabricated zero GPU-seconds or cost.
