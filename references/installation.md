# Install and check

Users can ask Codex:

> Use skill-installer to install the repository root (`--path . --name research-loop`)
> from Chen-HongRu/research_loop. Check the installed runtime. Do not start research yet.

Use the available skill-installer helper rather than downloading an isolated
SKILL.md. For a release or PR test use `--ref` with the exact commit/ref. The whole
root must be copied: `src/`, `scripts/`, `pyproject.toml`, `uv.lock`, references,
examples and docs must remain together. A GitHub archive needs no `.git` directory.
Do not install the previous `skills/research-loop` draft; it is not the distribution.

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

After installation, report the installed path and that the skill is available on
the next turn. If discovery is stale, restart Codex. When installation/setup is the
only request, stop here; do not consume research/model/compute budget.
