# Moving a Study to a Linux host

Prepare the pinned runtime with Python 3.12 and `uv sync --locked`. Verify the
remote ChatGPT login, exact model/effort, native sandbox and delegation before
moving control. The shell/search utilities used by prepared scripts must exist.
Keep the original scientific execution backend and authorization unless the user
explicitly changes them.

Preserve the Study and project absolute paths: saved scripts, native histories and
resource ledgers can refer to them. Copy current source edits as well as tracked
files; rebuild environments instead of copying platform-specific virtualenvs.
Canonical datasets and checkpoints can remain on the existing experiment backend.

Copy private files with ownership matching the remote service user. Host root access does not prove that a user-namespace sandbox can traverse files owned by another UID. Preserve restrictive modes instead of widening permissions.

The writer lock is host-local. Stop and unregister the original supervisor before
the final copy and remote admission. Confirm original jobs are terminal and settle
native processes; retain and reconcile any cleanup warning. Transfer the complete
Study, relative knowledge links, native session/database state, budget ledger and
its lock coherently after shutdown. Never run `init` or reset the ledger. Exclude
`native/auth.json`: the remote runtime recreates it against the remote user's
existing login. Do not transfer credentials through source archives or logs.

With the pinned Linux sandbox, denying native state beneath a read-only Study
mount can fail during Bubblewrap setup. Use a private native-state directory
outside the Study read root and link `STUDY/native` to it before configuration.
The existing configuration resolves that link and keeps the physical directory
denied; no permission widening is needed. Verify project/work access and denied
native/control access with synthetic files, plus one native delegation smoke.
This storage layout is deployment configuration, not a second runtime.

Resume the original Study explicitly with `run STUDY --continue` after confirming
there is one controller owner. Use a supervised service, retain original records
and inspect the actual resumed thread, resource balance and first live activity.
Research failure must not trigger an unconditional service restart. Do not reopen
an operator STOP through automatic restart.

Run the dashboard separately on loopback. If using a reverse-proxy path prefix,
retain its trailing slash and serve relative asset/API URLs. Keep existing HTTPS
and access control. Verify both the backend and external route without exposing
research artifacts or credentials publicly.
