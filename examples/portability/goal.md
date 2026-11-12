# Real repository portability audit

Audit this pinned Research Loop checkout on the current host. This is a real
software verification task using its actual code and tests, not a synthetic
arithmetic task or an algorithm-performance experiment. A matching fresh Study
will be run on the other operating system separately.

Use the default Direction → Exp → Synthesis Groups. Confirm this explicit task
without unrelated exploration. Exp owns implementation and its independent
Evaluate; Synthesis owns the final independently reviewed publication.

## Frozen scope

The only subject is `study_context.input.project_root`, at the revision recorded
in `study_context.input` or the Study's admitted task. Read its project metadata,
documentation, selected source and tests. Do not read other Studies, native
transcripts, credentials or unrelated home-directory contents. Do not modify the
checkout, install packages, use network/SSH, invoke models from scripts or use a
GPU. Dependencies are already installed by `uv sync --locked` before this Study.
Use the observed execution Python; unqualified `python` must resolve to it.

The exact test selection is:

```
tests/test_environment.py
tests/test_group_agents.py
tests/test_exp_workflow.py
tests/test_reference_workflow.py
tests/test_publication.py
```

This is software verification: no model loss, tensor mask, dataset split or time
alignment is involved. Do not invent those fields. The frozen source is input;
temporary files, subprocess outputs and JUnit are observations, not training data.

## One actual execution

Plan the verification using the supplied `examples/portability/audit.py` in the
subject checkout. Its deterministic Python implementation owns metadata, pytest
invocation, XML counts and report delivery. Do not rewrite, copy or replace it.
Prepare exactly one command: the observed Python runs that script with
`--output` set to Exp's work directory. Do not execute it during preparation.
The supplied program invokes the observed Python
with `-m pytest -q -p no:cacheprovider`, the exact five files above, JUnit output
and `--basetemp` both under Exp work, with the project as cwd. Disable bytecode
writes and inherited `PYTEST_ADDOPTS`. Use a 120-second pytest timeout. Do not run
pytest while preparing, perform a rehearsal, repair the subject or retry it.

Write `metrics.json` and a concise `report.md`. Metrics must record source
revision (from project Git HEAD), platform, Python executable/version, installed
pytest version, imported research_loop module path, actual test selection,
pytest exit code, elapsed seconds, tests/failures/errors/skipped, and JUnit/log
paths. Derive counts from the actual JUnit XML; never hardcode passing totals.
Use the recorded Git revision and environment metadata for identity; do not add
content scans or hashes of source, logs, JUnit or other produced artifacts.
Keep stdout/stderr in `pytest.log`. A normal completed pytest exit 0 or 1 means
the audit runner finished; preserve a nonzero pytest exit as a negative finding
in metrics, including test fixture errors recorded in JUnit. Runner/bootstrap
errors, pytest collection/internal/usage errors (exit 2–5), missing output or
timeout fail the runner. A successful runner receipt never implies test success.

The host executes the prepared command once. Independent Evaluate reads the
actual metrics, JUnit and log, checks their agreement and environment identity,
and reports positive AND negative evidence without executing pytest again.

## Completion and publication

Publish the honest observed result even when it contains test failures. A positive
compatibility finding requires pytest exit 0 and no failures/errors/skips. Missing
evidence is a blocker, never a pass. Distinguish successful audit execution from
the subject passing its tests. Do not claim this verifies every Linux/mac machine,
an active Study migration, provider parity, GPU behavior or general research gain.

For this one-task Study, stop after actual execution and independent analysis
are published. The admitted human request is completed only with the successful
runner receipt and independent evaluation cited in its evidence. Synthesis must
author a real index and finding, preserving limitations. No further experiment,
repair, autonomous round or unrelated benchmark is authorized by this task.
