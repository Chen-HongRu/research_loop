# Linux supervision

Install the locked runtime on the target host using
[installation](installation.md), its own Codex login, and already authorized
scientific resources. Prepare a fresh private run with paths valid on that host.
Moving older runs or rewriting frozen paths is not a supported migration route.
Canonical datasets/checkpoints may remain on their authorized backend as explicit
references; provisioning or copying them requires separate authority.

## Controller ownership

`research-loop start RUN` launches the prepared interpreter and source snapshot
in a detached process session. The launching Codex conversation may end without
owning that process. Query `status`, `controller.log`, and `completion.json` for
observed progress and terminal outcome. Detached launch is local process ownership,
not a guarantee of host availability or automatic recovery.

An existing process supervisor may own `research-loop start RUN --foreground`.
Both launch modes select the interpreter and Python source snapshot recorded in
`RUN/run.json`; `inspect` uses that implementation too. Inspect the actual process
and first recorded execution to verify the selected implementation. Keep one
controller owner; the writer lock is host-local and does not coordinate two hosts.
Stop and settle the old owner before any authorized ownership transfer.

Configure the supervisor to retain terminal failures and operator stop intent.
Research failure, STOP or unknown backend state must not trigger unconditional
restart. Explicit `research-loop resume RUN` starts a new recovery admission after
checking original operation receipts; it does not reset budgets, repeat unknown
submissions or restore interrupted Agent sessions. See
[control and recovery](operations.md#inputs-and-interventions).

No event-to-external-Codex wakeup is installed. Terminal completion records support
manual queries or an independently configured authorized supervisor integration.
Do not describe them as automatic diagnostic follow-up.

## Permissions and remote work

Preserve restrictive file ownership for the actual service user. Verify selected
project reads and work/scratch writes using synthetic files before a bounded live
model smoke. Workspace permissions do not hide other readable host files; keep
scientific evidence selection explicit and credentials/control state outside
writable ancestors. Never transfer authentication through source archives or logs.

Retain original resource ledgers and operation/job identities. Local cancellation
is not proof that a remote job stopped. Reconcile UNKNOWN receipts before any new
allocation. Remote command inputs/code must already exist under the authorized
backend; configuration does not install dependencies or deploy files.

The existing dashboard is a separate read-only loopback process. External proxying
and access control belong to the host deployment. Share only directories whose
private records are authorized for every viewer. Internal Group records are available through the application queries and the
read-only `/api/execution` endpoint described in
[operations](operations.md#queries-and-publication).
