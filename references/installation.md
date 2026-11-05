# Install and check

Users can ask Codex:

> Use skill-installer to install the repository root (`--path . --name research-loop`)
> from Chen-HongRu/research_loop. Check the installed runtime. Do not start research yet.

Use the available skill-installer helper rather than downloading an isolated
SKILL.md. For a release or PR test use `--ref` with the exact commit/ref. The whole
root must be copied: `src/`, `scripts/`, `pyproject.toml`, `uv.lock`, references,
examples and docs must remain together. A GitHub archive needs no `.git` directory.
Do not install the previous `skills/research-loop` draft; it is not the distribution.

If the helper's archive download fails due to local TLS/network configuration, use
its supported `--method git` with the chosen branch/tag and record the installed
Git revision. Do not disable TLS verification. Some helper versions cannot fetch
an arbitrary commit through shallow Git fallback; use a branch/tag containing the
intended revision and verify HEAD, or fix archive transport for commit-pinned installs.

Some helper versions implement `--path .` as a sparse root checkout and copy only
root files. If the newly installed directory contains `.git` but lacks `scripts/`
or `src/`, run `git -C /path/to/installed/research-loop sparse-checkout disable`.
This hydrates the same checked-out revision; verify HEAD and required files before
doctor. Do not apply it to the research target repository. If there is no Git
metadata, the archive/install is incomplete: retain it for inspection and reinstall
to a fresh destination rather than guessing a runtime.

The built-in installer's destination may vary by Codex version. Use the path it
actually reports and check discovery in the next turn. The currently documented
user discovery location is `~/.agents/skills`; some clients/helpers install under
`$CODEX_HOME/skills` (normally `~/.codex/skills`). If not discovered, link the installed
directory into the supported discovery location, without replacing an existing
skill. Do not change unrelated Codex settings. See [official skill docs](https://developers.openai.com/codex/skills).

Requires macOS/Linux, Python 3.11+, uv, Git and an installed, logged-in Codex CLI.
If a prerequisite is missing, have Codex install it using its supported installer
within the user's authorization, or report the exact unavailable prerequisite.
Authentication is interactive: direct the user to `codex login`; never read/copy
tokens or create keys for this workflow. `doctor` checks CLI login status without
reading credentials and prepares the locked Python environment:

```sh
python3 /path/to/installed/research-loop/scripts/research_loop.py doctor
```

The first runtime command uses `uv run --locked --project SKILL_ROOT` and may download
locked dependencies and create `.venv` in the installed directory. It needs network
and write access there; use normal permission approval if required. Subsequent
commands preserve the caller's working directory. No model calls occur in doctor;
it does not prove the requested model is available to the account, that a GPU is
accessible, or that a task-specific backend works. Only an authorized real run can
establish those conditions. Never advertise automatic model installation/login.

The controller starts child Codex CLI processes. A controller launched inside an
outer Codex command sandbox may be unable to initialize that child's app server
or write the CLI state database even when `doctor` and login checks pass. If that
specific failure occurs, retain the attempt and request normal host execution
approval for the same controller command. Keep the controller's child-stage
sandboxes enabled; do not use danger-full-access or bypass approval flags. A failed
read-only selection can resume through `run` within the original budget. Inspect
Execute effects before any recovery involving execution. If approval is unavailable,
report the precise blocker rather than repeating commands or claiming success.

SQLite runtime applications are explicitly trusted and configured separately from
the installed skill. Keep a versioned immutable application module and private
configuration outside the skill and repository; see [concurrent runtime](../docs/concurrent-runtime.md).
The existing legacy Harness does not automatically become a SQLite application.

After installation, report the installed path and that the skill is available on
the next turn. If discovery is stale, restart Codex. When installation/setup is the
only request, stop here; do not consume research/model/compute budget.
