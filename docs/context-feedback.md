# Context and Feedback

This is stage 2 of [#77](https://github.com/Chen-HongRu/research_loop/issues/77).
The input facade runs on new calls in the existing serial Study controller.
The transactional Updater is available for SQLite schema-8 Studies. The
[backend lifecycle](backend-lifecycle.md) adds operation admission and trusted
receipt intake; the [concurrent coordinator](concurrent-runtime.md) drives these
interfaces. The [SQLite CLI and dashboard](concurrent-runtime.md) are available; [migration](runtime-migration.md) and deployment require operator evidence. The
[publication interface](knowledge-publication.md) accepts Scout/Review knowledge
and builds derived views from registered measurements and eligibility revisions.

## Frozen input

```text
ContextSnapshot.capture(...) -> ContextSnapshot
build_context(snapshot, role, task, policy_ref, budget, policy=...) -> ContextPackage
```

A snapshot contains the Study identity, relevant revisions, inputs, one source
record per top-level input field, protected keys and permitted evidence references.
Source records contain `id`, `key`, `ref`, `revision`, `reason`, `allow_feedback`.
Revisions are opaque JSON version identifiers supplied by the trusted controller.
Snapshot and package values are captured immutably; accessors return detached
copies. The default policy preserves all permitted input values and their order.

The optional policy receives copies of eligible inputs, source metadata and the
context selection budget (`policy(inputs, sources, budget)`) and
returns field selections. It may include a field, omit an optional field, or keep
a prefix of optional text with an explicit character count and reason. It cannot
rewrite values, introduce fields, grant evidence access or remove protected
constraints. Goal, Plan, evaluation, permissions and authorization budget fields
are protected. The runtime also protects the exact task envelope. Materials
marked `allow_feedback=false` never reach the policy or the resulting context.

The package contains `context` and `manifest`. The manifest records Study/task/
knowledge/execution versions, role, policy version, sources, selection reasons,
provided/omitted/truncated dispositions, context quota and permitted evidence.
The token estimate is the serialized context's character count divided by four,
rounded up. It excludes protocol instructions, schema and tools; it is not measured
model usage. `actual_usage` remains null. No summarization call or global token
limit is introduced.

`build_context(..., budget=...)` supplies the **context selection budget**. In the SQLite
runtime it must match immutable `config.context_budget`, which defaults to `{}`.
It is recorded and passed to the selected policy; this facade does not interpret
arbitrary budget keys or enforce a universal token ceiling. A policy with a named
limit must implement that limit or fail when protected inputs cannot fit.
This is separate from `context.budget`, which contains the Study's authoritative
iteration/call/time limits and admission deadline. Context policy does not change
those limits, spent counters or STOP.

Policies installed as Python callbacks and source catalogs are trusted controller
code. The interface does not sandbox malicious Python or authenticate arbitrary
source descriptions. Untrusted Agent decisions are data proposals, never live
coordinator handles. Backend/tool boundaries remain responsible for preventing
access to final-only data outside the provided context.

## Role access in a SQLite Study

Set `role_access` in the Study configuration at creation. Each named role has
one rule shared by context construction, binding and validated output admission.
Omitted roles keep their existing behavior. This example gives Review its own
accepted Plan, Execute, collected reports and eligible measurements for the
iteration. Advocate receives the frozen planning round and published research.

```yaml
role_access:
  review:
    published_history: false
    context_sources: []
    permissions: []
    actions: [knowledge, reviews]
  advocate:
    published_history: true
    actions: [proposals]
```

Native Pi roles also accept `inputs`, `knowledge_bases` and `tools` as
authorization ceilings. Their default input contains library descriptors for
on-demand retrieval. An explicit history provider can select the older direct
projection when permitted. See [Study knowledge](study-knowledge.md).

For the generic context builder, `published_history` defaults to true and
governs published research, Frontiers, historical measurements and imported
history. The task, role-specific control inputs, and the planning pool for
Advocate/Judge remain authoritative inputs. `context_sources` narrows the
operator's named sources by intersecting with their existing role and
feedback grants. `permissions` selects keys from the Study permission map
and preserves their values. An empty list selects none. A configured role
may cite only references represented by visible source fields.

`actions` restricts nonempty sections of accepted output. Plan can propose
`operations`; Execute can request `operations` and supply `source_edits`;
Review can publish `knowledge`, `reviews` and `corrections`; Scout can publish
`knowledge`; Advocate can make `proposals`; Judge can submit `changes`.
A role receives its effective `allowed_actions` in protected context. The
controller checks normalized output before admitting operations or applying
updates. The script application checks source edits against this same action list when validating proposed files. Custom applications implement their own backend-specific edit checks. Study grants, protected sources and backend limits still apply.
These rules govern frozen inputs and controller actions. Interactive Pi search,
filesystem access and operating-system isolation have separate enforcement
boundaries.

For a configured role, the controller freezes the first selected package per
attempt and atomically records its artifact reference and identity in
`context-grants/`. Binding accepts that exact package, including every source
reference and the supplied content. Rebuilding the attempt reuses the same
selection. These receipts belong to the trusted Study control directory.

To revise an existing knowledge entry, the configured role must have received
the full published `research` field. The Publisher checks the target's revision
at the frozen publication version and again against its current revision.
An omitted or truncated research field allows new entries only. A conflict
leaves the target unchanged. On upgrade, legacy **configured-role** bindings
without an issuance receipt cannot authorize new feedback or a Review retry
from that package. Use a fresh invocation for a new decision. Previously
published receipts remain replayable; unconfigured roles retain their prior
binding contract.

## Validated output

```text
Updater(coordinator, validators={role: validator})
bind_context(call_identity, package) -> context_ref
validate_output(call_identity, output, evidence_refs) -> UpdateProposal
apply_update(proposal, expected_revisions) -> UpdateReceipt
```

`CallIdentity.from_attempt(study_id, attempt)` binds the Study, attempt, role,
iteration, expected revision, owner epoch and worker token. Context binding occurs
once before dispatch. The updater compares frozen goal/evaluation/permissions,
task, authorization limits and accepted Plan with authoritative records. Execute
and Review require the original normalized, accepted Plan. Plan and Execute
contexts additionally protect the exact configured `execution_backends`,
`resource_pools`, and `operation_authorizations` dictionaries when present.
Selection policy cannot omit or rewrite backend identities, resource limits or
cost authority. These inputs do not make an Agent a backend/controller.
The initial revision
map is `{study: 1, task: revision-or-null, iteration: revision-or-null, knowledge: version}`.
Knowledge starts at zero and advances by publication sequence, independently of
iteration identity. A snapshot captured before another independent publication
may still bind and finish. Its knowledge version remains frozen; updates to an
existing knowledge or eligibility record require that target's expected revision.

Planning calls additionally protect their exact captured task pool, in-flight
iteration metadata and event range. Judge inputs include the two terminal
Advocate results or failure summaries. These identities come from the reserved
planning slots, not the worker's proposed replacements. Research text and
Frontiers use separate frozen knowledge sources and remain selectable by context
policy. Claimed task envelopes never change when pending tasks are revised.

In the SQLite updater, permitted evidence references name existing immutable
`artifacts/<uuid>.json` records. Their file identities are frozen at binding, so
replacement between dispatch and the first response is rejected. Source metadata
may also describe symbolic controller inputs or external provenance, but those
descriptions alone do not grant access or become locally verified evidence.

Each role requires an explicitly registered trusted validator with signature
`validator(output, context) -> normalized_output`. Validation checks the bound
identity and permitted input evidence references and returns an immutable proposal;
it does not advance state. Unknown references, changed bindings, stale revisions
and direct control-state changes are rejected. A structural validator accepting a
report does not prove its scientific claim. Claims, measurements and corrections
retain their existing scientific validators and backend evidence requirements.

For an uncommitted proposal, `apply_update` revalidates raw output and verifies
normalization, expected revisions and binding. A forged in-memory proposal does
not bypass validation. The raw/normalized proposal is published to an immutable
file before a short transaction records acceptance and advances only the associated
iteration. For Execute, the updater validates a finite optional `operations`
list against the accepted Plan and configured backend, resource and cost
authorizations before acceptance. Nonempty operations and their immutable request
references are admitted in the same acceptance transaction; the Execute is
accepted before external completion. Later collection never amends that sealed
output. Review reservation remains blocked until operation knowledge and
collection are terminal, evaluator intake is resolved and feedback is sealed;
fee settlement is an independent ledger. Context-bound attempts cannot use raw
`accept_attempt` to bypass this path. Rejection preserves the reservation and does not automatically retry work.

An exact committed proposal returns its original receipt, even after restart or a
validator upgrade. It does not invoke the current validator, allocate another
output file or advance the iteration again. Different output or identity conflicts;
unresolved prior-owner attempts remain fenced. STOP blocks new admissions while
allowing an already reserved result to be recorded.

Raw context and proposal records live in immutable files. SQLite stores their
references, identity metadata and acceptance records. The updater reads specific
control records to perform the requested validation or receipt lookup outside
write transactions. It does not scan or hash datasets, checkpoints or evidence
contents. Each permitted evidence record receives a bounded metadata check at
binding; cited records are checked against that identity at validation/commit.
This protects the frozen source version: a synthetic replacement after binding
was observed to be accepted before this check was added. These checks run in the
controller, not per training rank or loader worker; they read zero evidence or
dataset content bytes. Runtime latency has not been benchmarked.

## Existing Study calls

New serial Agent attempts save `context-manifest.json` beside their unchanged
`context.json`, schema and prompt. `legacy-context-v1` captures the existing role
differences and controller-selected inputs. Every supplied field remains protected
and provided; no extra Agent invocation is made. Captured JSON field references
are provenance and citation scope, not a new OS filesystem sandbox.

Review identifies the knowledge snapshot at review start; execution continuations
retain the originating scientific input references. Dynamic controller-derived
fields have controller source identities and the exact call revision. Cached
successful attempts keep their original manifest. Historical attempts without
one remain readable; their provenance is not retroactively invented.

SQLite schema 8 contains binding, update, publication, planning and operation
records, including evaluator intake and separate resource/cost holds. Earlier SQLite
test Studies must be retained as evidence or recreated in a new directory. No
automatic conversion of existing Studies occurs in this stage.

`RuntimeContexts` builds concurrent role inputs from frozen control fields,
accepted Plan/Execute outputs and published knowledge. Review also receives its
own feedback-permitted, unpublished measurements with their exact evaluator
receipt references protected from policy omission. Other unpublished measurements
and denied evaluator bodies are excluded. Once bound, the selected package is
reused even if policy or published knowledge later changes. Managed dispatches
can accept only the exact result in their durable worker completion record.

Operator-supplied baseline source and research contracts can be published as
small immutable JSON-object control artifacts and selected with
`config.context_sources`. Each entry has exactly `name`, `ref`, `identity`,
`revision`, `roles`, `allow_feedback` and `required`. `ref` is a Study-local
`artifacts/<32-hex>.json` reference; `identity` contains its captured `device`,
`inode`, `size` and `mtime_ns`. `revision` is an explicit finite JSON version;
`roles` is a nonempty subset of `plan`, `execute`, `review`, `scout`, `advocate`
and `judge`. Use a unique simple name such as `baseline` and store source text
inside the object, for example `{"files": [{"path": "baseline.py", "content": "..."}]}`.

Eligible objects enter both ordinary and planning contexts as `provided:<name>`
and their original refs enter the evidence allowlist. Required objects are
protected; optional objects may be omitted, but neither may be rewritten or
summarized. Denied and other-role sources retain only explanatory manifest
metadata and never have their bodies read. Updater independently checks content,
configured version and stat identity, with metadata-only rechecks at commit.
These inputs are explicit control artifacts, not dataset/checkpoint readers or
scientific verification. Omitting `context_sources` preserves existing behavior.

Own Review can additionally receive explicitly feedback-permitted unscored
reports from its accepted operation collections, under
`evidence:<operation-id>:<name>`. These immutable JSON report objects retain
unverified-observation provenance and original evidence refs. Denied reports are
never opened; other iterations and roles do not receive their bodies. Source
content and collection bindings are independently checked before acceptance.
Report citations can support only interpretation knowledge, never new verified
measurements or Frontier entries. See [collection and Review](backend-lifecycle.md#collection-and-review).

## Local API smoke

This disposable example reserves one synthetic Scout call and accepts a strictly
checked note. It calls no model or backend and makes no research claim.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from research_loop.runtime import initialize
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.updates import CallIdentity, Updater


def validate_note(output, context):
    if set(output) != {"note"} or not isinstance(output["note"], str):
        raise ValueError("Expected one textual note")
    return output


with TemporaryDirectory() as directory:
    config = {"goal": "Synthetic interface check", "permissions": {}}
    store = initialize(Path(directory), config=config)
    with store.coordinator() as owner:
        attempt = owner.begin_attempt("scout", request_id="example-scout-1")
        state = owner.snapshot()
        call = CallIdentity.from_attempt(state["study_id"], attempt)
        inputs = {**config, "task": None, "budget": state["limits"]}
        revisions = {"study": 1, "task": None, "iteration": None, "knowledge": 0}
        sources = [
            {"id": f"controller:{key}", "key": key, "ref": f"input:{key}",
             "revision": 1, "reason": "Fixed control input", "allow_feedback": True}
            for key in inputs
        ]
        snapshot = ContextSnapshot.capture(
            study_id=state["study_id"], revisions=revisions, inputs=inputs,
            sources=sources, protected_keys=list(inputs), allowed_evidence=[],
        )
        package = build_context(snapshot, "scout", None, "example-v1", {})
        updater = Updater(owner, validators={"scout": validate_note})
        updater.bind_context(call, package)
        proposal = updater.validate_output(call, {"note": "Synthetic note"}, [])
        receipt = updater.apply_update(proposal, revisions)
        assert updater.apply_update(proposal, revisions) == receipt
        assert owner.snapshot()["counters"]["calls"] == 1
```
