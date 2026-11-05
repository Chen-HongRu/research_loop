# Explicit runtime migration

The schema 8 migration commands transfer an explicitly translated legacy Study to
SQLite authority. Preview is read-only; prepare, commit and activate require an
authorized trusted operator. There is no automatic legacy semantic or backend
translator. Preserve the legacy records and do not treat the existence of a
migration command as evidence that a deployment has migrated.

```sh
research-loop runtime migrate preview /path/to/study --output /path/to/private-inventory.json
research-loop runtime migrate prepare /path/to/study --import-id example-import \
  --inventory /path/to/private-inventory.json --translation /path/to/translation.json \
  --config /path/to/config.json --fence /path/to/fence.json \
  --deployment /path/to/deployment.json --application /path/to/versioned/application.py \
  --application-config /path/to/private-application-config.json
research-loop runtime migrate commit /path/to/study example-import
research-loop runtime migrate resolve /path/to/study example-import --evidence /path/to/resolutions.json
research-loop runtime migrate bind-execute /path/to/study example-import --evidence /path/to/execute-binding.json
research-loop runtime migrate activate /path/to/study example-import --evidence /path/to/activation.json
```

The application-config option is optional. Use `--help` for each command and
keep inventory, translation, application configuration, evidence and runtime
artifacts private and outside Git. Preview captures exact legacy control records
and reports blockers; it makes no cutover. The operator must inspect these records,
construct a translation with explicit source references and resolve its blockers.
For control documents outside the Study, preview accepts `--annex-spec FILE`, a
JSON object mapping explicit logical names to absolute control-document paths.
Use names without `../`; the capture stays private and its source bindings are
verified. The [ProTok migration adapter](../integrations/protok/README.md) offers
a pure translator and composable cleanup with strict original-source binding.
It preserves original grants, including uncapped `max_operations: null`, while
retaining original cost and time constraints.
ProTok translation requires complete captured historical script coverage and
binds job accounting to the original execution and grant. A nested `original_job`
no-submission assertion must match the exact captured job and authorization; it
remains an attributed legacy assertion, not proof of current sender retirement.
The import retains preview blockers. Use `runtime migrate resolve STUDY IMPORT_ID
--evidence FILE` for explicit reviewed resolutions: the evidence names the import,
expected revision, and resolutions containing each exact blocker, reason and
evidence references. Resolutions are retained in an immutable proof ledger as
trusted operator assertions; they do not authenticate the underlying evidence.
An untranslated execution cannot be cleared without a retained execution mapping.
There is no force flag.
For an unavailable legacy Execute, `bind-execute` takes an evidence object with
`import_id`, `attempt_id`, `expected_revision`, `normalized_output`, and
`evidence_refs`. This trusted original-effect settlement creates no new Agent
completion, training or measurements. Repeating identical evidence is idempotent
and leaves call counters unchanged. Operation readiness, holds and feedback may
still block Review.
Pending human task identities, effective priorities and cancellation states must
be preserved. Translated phase bindings are marked legacy origin, not fresh model
calls. Historical records remain separate provenance, not new publications or
measurements. Imported operations permanently forbid new submission; reconcile
the original execution, never replace it with another submit.
Every translated operation must include `unexpected_job_ids`, including an empty
list when none are known. Unexpected job identities quarantine affected aggregate
resource pools until reconciled.

The import preserves compact legacy research as an immutable context artifact,
labeled `origin: legacy_v3` and `verification: original_legacy_claims`. It is
archived by default, not automatically available as feedback. Set the frozen
`config.legacy_history_feedback` to `true` only after explicitly reviewing and
permitting that source for feedback. Original numeric Frontiers remain in the
exact archive; they are not exposed as new measurements or Frontiers.

The frozen target configuration must preserve the exact legacy goal, global
evaluation and `allow_write` permission. Counters, original limits and STOP are
carried forward. The application, backend mapping and translated operational
facts are explicit operator choices; their schemas do not prove external facts.
If an outer legacy supervisor imposed an admission deadline, map it in
`config.migration_admission_deadline` as a finite absolute UTC timestamp. The
optional mapping can impose a deadline where none was recorded or tighten an
existing one; it cannot extend the original deadline.
The fence document identifies the source and records that old launchers are
disabled and senders retired, with evidence references. A human operator must
actually fence old services and verify that statement before cutover; a certificate
alone does not stop a process or job. The deployment document binds a full immutable
commit, schema, versioned environment and application identity. Deploy the matching
format entry points and versioned application explicitly; do not overwrite an active
runtime. Module dependencies and environment remain part of the trusted version
contract.

Prepare holds both legacy controller and task-queue locks, verifies the captured
inventory and stages the import without changing legacy authority. Commit also
holds these locks and publishes a fail-closed runtime marker and database through
resumable steps. On interruption, rerun the same import ID and identical request;
do not remove markers or start an old launcher to force progress. The committed
Study starts in `inspect_only`, independently of preserved STOP. Inspection and
cleanup are allowed; new admission requires explicit activation, and activation
never clears STOP or existing holds.

Before activation, reconcile imported operations through actual backend receipts
and resolve all admission blockers. Activation evidence names the import and
expected revision, authorization and time evidence references, reconciliation
references, and a finite nonnegative `additional_elapsed_seconds` charge for
unknown legacy wall time. References and their shape do not establish the truth
of the evidence. Review actual jobs, physical resource release and fee settlement
separately; local process retirement is not proof of external completion. Activation
preserves remaining original authorization and does not grant new resources.

## Local acceptance

```sh
uv run python examples/runtime_acceptance.py
```

A local run exercised a temporary Git workspace and two actual CPU subprocess
jobs with peak overlap of two, a coordinator restart with zero new submissions,
collection retry and explicit Review retry. Agent outputs and evaluations were
synthetic. The local backend service remained alive across the coordinator restart;
this does not validate service-loss recovery, a live backend, GPUs or a remote
migration. Remote deployment state must be inventoried and verified separately.
