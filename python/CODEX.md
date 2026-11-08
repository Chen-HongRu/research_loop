# Codex role execution

`CodexRoleRunner` executes the existing research roles through the official Python
Codex SDK and ChatGPT subscription authentication. The application still owns the
task pool, dependencies, host evidence, independent Review, resource accounting,
knowledge publication and explicit settled handoffs. Existing `Agent` and
`FunctionTool` objects describe roles and handlers; they do not invoke the Agents
SDK Runner on this path.

The implementation pins `openai-codex` and its CLI runtime to 0.167.0. It uses two
version-specific SDK seams for experimental dynamic tools: the client request
handler and raw thread parameters. Upgrade them together and rerun the boundary
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

Each role receives its supplied function tools and one structured submission
tool. The host validates submission arguments with the role's output type,
guardrails and stage validator. Rejected validation returns concrete feedback
within the same native thread. A successful submission is sealed: repeated
identical submission returns its receipt, and further ordinary tools cannot run.
The final natural-language response provides the human summary; it is not the
structured delivery authority.

Stage validators run once on the Codex path. Knowledge publication is marked as
effectful so a publication error returns to the controller instead of being
blindly retried inside a tool call. An accepted submission remains authoritative
if only the subsequent summary generation fails.

Native filesystem and network access are denied. The adapter uses the pinned
runtime's original model metadata with only built-in tool capabilities removed,
disables environment access and user plugins/MCP/skills, and exposes the supplied
host-controlled tools. The native runtime reads subscription credentials through
an isolated configuration; Python does not extract credentials or pass provider
API keys. Tool permissions and resource receipts remain the application's duty.

## Continuation and observation

Native thread references support same-stage correction. Explicit cross-process
continuation requires the retained state directory and a settled application
handoff; neither session persistence nor this adapter reconciles unknown external
effects. A completed execution can still feed a fresh independent Review without
repeating the experiment. Old Agents SDK histories must be archived as evidence,
not treated as native Codex threads or replayed tool requests.

The normal local trace records role/tool activity. The separate private event
file retains Codex events and native thread/turn identities. Model-response counts
and provider costs must be reported as unmeasured when unavailable. These files
can contain private research and must stay outside commits.
