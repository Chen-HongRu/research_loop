# Codex skill verification

Verified on 2026-11-03, macOS/arm64, Codex CLI 0.156.0, uv 0.11.19,
controller Python 3.12.5. This is an installation/lifecycle smoke, not evidence
of general research benefit or a comparison of research strategies.

## Installation and runtime

- Installed the public GitHub task branch using the built-in skill-installer,
  repository root `--path . --name research-loop`.
- Archive transport failed locally with a TLS EOF. Git installation succeeded
  but its sparse root omitted subdirectories; `git sparse-checkout disable`
  materialized the same checked-out revision. Both observed cases are covered
  by the installation instructions; TLS verification was not disabled.
- Installed and checked a separate replacement runtime before switching the
  discovery link; retained the original runtime outside skill discovery.
- Doctor passed Python, uv, Git, Codex, login and bundled CLI checks. It made
  no model call and correctly left model/backend availability unverified.
- A separate `git archive` export without `.git` ran the labeled scripted
  offline demo and exposed its graph from an external working directory. This
  checks archive layout and path resolution; its stage responses are synthetic.

## Actual Codex workflow

The account rejected `gpt-6.1-sol` with model_not_found. Separate minimal requests
for the allowed Astra and Luna names succeeded. The research smoke explicitly
requested `gpt-6-astra`/medium; no older model or alternate provider was used.
These are client/request observations, not independent verification of the
server's internal model implementation.

A natural-language request in an isolated repository containing only a README
caused Codex to generate a real Python cancellation diagnostic, format/check it,
run its CPU smoke, create a separate clean Git baseline and write the goal/Study.
It tested the six permutations of `[1e16, 1.0, -1e16]` using explicit sequential
float addition, `math.fsum` and exact represented-input Fraction references.
The existing README was preserved. No private data, GPU or remote job was used.

The outer command sandbox could run doctor but blocked nested CLI app-server
startup. The failed read-only selection was retained. Normal approved host
execution resumed that same Study within its original budgets and preserved all
child-stage sandboxes. The first real investigation completed Execute, independent
Review and publication. Review kept the goal `not_met` and paused its route pending
a separately requested scaling investigation. Its qualitative Frontier was empty.

Another Codex interaction located the installed skill by name, read status,
knowledge, graph and frontiers, submitted a synthetic queue-only request, changed
its priority, cancelled it before binding and verified cancellation. It then
submitted the real scaling follow-up against the published `0001` source version.
Queue operations did not launch an experiment or alter the original budgets.

The first Review stop exposed the missing continuation transition: plain `run`
does not reopen stopped research. The new explicit `resume --reason` records the
operator's request while retaining counters, evidence and route states. Eligibility
checks protect this observed case from accidental budget/goal/effect bypass: they
read only state/configuration and small iteration metadata plus file existence;
no dataset/checkpoint is read. They run once per operator resume, never per
training rank, worker or batch. Exact metadata bytes/time were not measured.

The replacement installed runtime (`325114e`) resumed the same Study and consumed
the queued follow-up through Plan → Execute → Review → publication. Its modified
script produced all 18 requested scale/permutation rows. Review marked the bounded
goal met and the Study stopped after two published investigations. The final
controller call count was 10 of 12, including the retained startup failure;
outer Codex interactions and model-availability probes are separate account calls,
not part of that Study counter. Total account cost was not measured.
Final status/graph/knowledge/frontiers queries succeeded through the installed
launcher. Another `run` on the stopped Study made no extra call; `resume` refused
the completed goal and left its counters unchanged.

Execute encountered uv/uvx cache write restrictions in its sandbox. It preserved
the failure diagnostics and used the explicitly prepared interpreter and local
Ruff installation rather than downloading dependencies or relaxing permissions.
This confirms the need to prepare actual execution access, beyond host smoke.

## Deterministic checks and remaining coverage

- Ruff format/check passed for all changed Python files.
- Skill Creator validation passed for the root skill.
- Main unittest suite: 338 tests, one skipped. New tests cover relocated launcher
  arguments/cwd, incomplete installation, login failure without model calls,
  and reviewed-stop resumption with unchanged evidence/budgets and rejection of
  ineligible or exhausted states.
- Independent read-only reviews checked installation/adapter invocation and the
  resume state transition. Their agreement supplements executable tests.
- The repository has no CI workflow; these are local verification results.

Raw Agent transcripts, fixture repositories and Study outputs remain local and
are not included in the PR. Remote leases, GPUs, unknown-effect Execute recovery,
long training, Scout and Meta Loop were not exercised in this live smoke; their
existing protocol/tests do not establish live installation coverage. Actual
process cancellation/recovery is covered by the existing scripted CLI tests,
not by interrupting a live paid model execution here.
