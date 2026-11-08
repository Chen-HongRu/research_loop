# Codex role execution

`CodexRoleRunner` executes the existing research roles through the official Python
Codex SDK and ChatGPT subscription authentication. The application still owns the
task pool, dependencies, host evidence, independent Review, resource accounting,
knowledge publication and explicit settled handoffs. Existing `Agent` and
`FunctionTool` objects describe roles and handlers; they do not invoke the Agents
SDK Runner on this path.

The implementation pins `openai-codex` and its CLI runtime to 0.167.0. It uses two
version-specific SDK seams for experimental dynamic tools, native turn options
and managed-terminal control. Upgrade them together and rerun the boundary
tests before changing the pin.

## Execution

Use `async with CodexRoleRunner(...) as backend` and pass
`role_runner=backend.run` to `run_research`. Supply a private `event_path`, an
isolated `cwd`, and a private `state_dir` outside Git. A supplied state directory
preserves native session files for explicit continuation; the temporary default
does not. Sign in with Codex before starting. The runtime verifies ChatGPT
authentication and availability of exactly `gpt-6.1-sol` with `medium` reasoning;
there is no API-key, model or provider fallback.

The Codex SDK does not expose an exact model-response limit equivalent to
Agents SDK `max_turns`. `allow_native_response_budget=True` explicitly authorizes
native execution boundaries; it does not enforce or claim an equivalent limit.
The application must retain its authorized resource limits, progress diagnosis,
stop/cancel handling and subscription-usage blocking policy.

## Tools and delivery

Each role uses Codex's native project search, shell, file and patch tools, plus
its supplied application function tools and one structured submission tool.
`project_roots` grants project-wide reading. `role_workspace(agent)` supplies a
real `cwd`, additional `readable_roots`, permitted `writable_roots`, and optional
`web_search=True` and `env={"PATH": "/path/to/project-env/bin:..."}`. Only
PATH can be supplied through `env`; it is included in the session binding.
Native model shell tools default to non-login shells (`allow_login_shell=false`)
to preserve that PATH. Changing it requires a new session. Leave writes empty for read-only roles; grant only task output
directories or the Knowledge Manager's private draft where writes are needed.
Agents retrieve context from real files; a virtual read/write/bash adapter is
not required. The application still owns remote resources and their budgets.

The host validates submission arguments with the role's output type and
guardrails. Pure validation failures return concrete feedback within the same
native thread. A successful submission locks its result: repeated identical
submission returns the receipt, and further application function tools cannot
run. Native tools can still run until the turn ends, so the submitted result is
not a snapshot of files at the tool-call instant. The final natural-language
response provides the human summary.

Stage validators run once on the Codex path. A validator marked
`codex_side_effecting` (including knowledge publication) runs only after the turn
ends, managed background terminals are terminated and confirmed empty, and the
role's client closes. It does not run inside the submission callback. Drain or
publication failures return to the controller and are not hidden as a missing
summary. Applications should publish an independent copy of a Manager draft so
old open file descriptors cannot modify the published version. An empty managed
terminal list does not certify that every detached operating-system descendant
has exited.

Native environment access and original model tool metadata are retained. Native
permission profiles grant project reads and explicitly selected writes, while
shell network access and credential/state-directory reads are denied. Scout can
use native web search when enabled for its workspace. User plugins, MCP servers,
hooks and automatic skill discovery are not loaded by this backend. The native
runtime reads subscription credentials through an isolated configuration; Python
does not extract credentials or pass provider API keys.

`on_event(agent, event)` receives native stream events and managed-terminal
control receipts. Completed `commandExecution` items expose their actual status,
exit code and working directory. Applications must distinguish an Execute
process from a Planner's successful file inspection, and verify the frozen
artifacts independently. File-change events are provenance, not proof that a
measurement completed. Background-terminal control receipts do not manufacture
a successful process exit. Native command and patch activity also appears in
the normal role/tool trace.

## Continuation and observation

Native thread references support same-stage correction. Explicit cross-process
continuation requires the retained state directory and a settled application
handoff; neither session persistence nor this adapter reconciles unknown external
effects. Workspace permissions and tool signatures are part of the thread's
binding; a permission change requires an explicit new-session handoff.
A completed execution can still feed a fresh independent Review without
repeating the experiment. Old Agents SDK histories must be archived as evidence,
not treated as native Codex threads or replayed tool requests.

An explicit `initial_stage_inputs` entry with `phase="collect_evidence"`, a frozen
`plan` and confirmed `execute_summary` resumes host collection without calling
Plan or Execute again. This is for a reconciled collection failure, not for
repeating or assuming the outcome of unknown external work.

The normal local trace records role/tool activity. The separate private event
file retains Codex events and native thread/turn identities. Model-response counts
and provider costs must be reported as unmeasured when unavailable. These files
can contain private research and must stay outside commits.
