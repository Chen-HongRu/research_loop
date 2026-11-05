# Live Study Knowledge

The native and script SQLite applications give Advocate, Judge, Plan, Execute,
Review, Scout and Knowledge Manager the same read contract. Pi's built-in
`ls`, `find`, `grep` and `read` operate on `knowledge/study/files/`. Every
authorized role can list and read the entire published file tree. For example,
`grep` can find a result in `notes/run-3.txt` even when the Manager omitted it
from the Index or deleted the Index entirely. File names, nesting and contents
are up to the Manager. A role receives its instructions, control inputs,
directory grants and submission schema at dispatch. Its next read can see a
new publication, even during one recursive search.

A planning round protects its proposal inputs and the task IDs and revisions
shown to Judge. Claim selects a task revision. Execute receives the approved
Plan and frozen evaluation; Review receives that Plan, the accepted execution
and the issuing experiment's admissible evidence. Each can consult newly
published knowledge. A knowledge publication neither restarts these calls nor
amends an experiment contract. Scout appends observations with immutable
Evidence IDs. A correction uses a new Evidence ID.

## Directory grants

The [native definitions](../integrations/native_runtime/roles/) and
[script definitions](../integrations/script_runtime/roles/) declare library
aliases and enabled tools. The default is the Study library. Operators can
add a read-only peer directory at an explicitly authorized **publication
version**. This mount boundary is part of the grant; it does not pin the
Study's own live knowledge. For example:

```yaml
knowledge_libraries:
  - alias: literature
    path: /path/to/reference-study
    version: 1
    description: Published reference material
role_access:
  plan:
    knowledge_bases: [study, literature]
    tools: [ls, find, grep, read, submit_result]
role_definitions:
  plan:
    knowledge_bases: [study, literature]
    tools: [ls, find, grep, read, submit_result]
```

The model sees relative paths such as `knowledge/literature/`. The host
constrains the whole Pi process. Only the Manager receives native `edit` and
`write` for its private `draft/` directory. Canonical revision files, private
Study records, sibling drafts and the operator's Pi profile are outside the
granted roots. See the [native application requirements](../integrations/native_runtime/README.md).

Directory access also bounds what a role may disclose. Because a search can
return text without naming it in the final answer, derived material inherits
the permissions of the directories and restricted inputs available to its
producer. All roles granted the Study directory see the same files. A Manager
can publish material derived from a peer only when every Study reader has an
explicit grant to that peer in `role_access`. Changing a future call's role
definition leaves this publication audience fixed. The controller checks explicit
citations against library identity, revision and source permissions.

## Evidence, drafts and publication

A trusted producer can append evidence directly:

```python
from research_loop.runtime.knowledge import KnowledgeBase

library = KnowledgeBase(study_root)
ref = library.publish_evidence(
    "E1", "experiment_receipt", {"observation": "Synthetic result"}
)
```

Submitting the same immutable record twice is idempotent. The ingestion
sequence is a maintenance cursor. A maintenance call receives its permitted
Evidence batch and the library version it is editing. The Manager uses Pi
`write` or `edit` to stage files under `draft/`. A staged file replaces the
published file with the same relative path. Other files carry forward unless
listed in `remove_paths`. For example, after writing `draft/notes/run-3.txt`,
the Manager can publish it, remove an obsolete Index and acknowledge its batch:

```json
{"output":{"processed_through":1,"remove_paths":["index.md"]},"evidence_refs":[]}
```

An empty draft can acknowledge Evidence by advancing the cursor. On publication,
the controller checks the write baseline and directory permissions, records the
new revision and activates the whole directory in one switch. Files keep their
original bytes. The controller accepts an incomplete or stale Index.
A concurrent edit from the same baseline is rejected. Rejected drafts leave
their Evidence pending. If directory activation fails after SQLite accepts a
revision, the old directory stays readable. Status shows the pending activation
and the next live open retries the accepted revision.
To request another bounded maintenance attempt:

```sh
uv run research-loop runtime maintain "$STUDY" --request-id retry-1
uv run research-loop runtime run "$STUDY"
```

## Inspect a decision or historical revision

```sh
uv run research-loop runtime status "$STUDY"
uv run research-loop runtime context "$STUDY" "$ATTEMPT"
uv run research-loop runtime context "$STUDY" "$ATTEMPT" --expand
uv run research-loop runtime knowledge-read "$STUDY" "$ATTEMPT" notes/run-3.txt --version 7 --with-sources
uv run research-loop runtime knowledge-read "$STUDY" "$ATTEMPT" --alias "$ALIAS" --reference "$REF_JSON"
```

Each invocation links its role and task/iteration to its structured decision
and explicit evidence references. For immutable library records use the
`kb:<library_id>:<kind>:<id>@<revision>` citation supplied with an input or listed by path in
`knowledge/<alias>/revisions.json`. Browsing `files/` works independently of this
citation list and any Manager-authored Index.
The CLI and read-only dashboard show the main library version at dispatch and
at terminal observation. For example, **start 7, end 9** means those versions
were published at the two endpoints. The role might have read none, one or
several of them. A filtered search, directory listing or partial file read
has no individual version receipt. Missing historical endpoints stay
`null` / **Not recorded**. An explicit historical lookup returns the requested
archived content within the recorded role's authorization. Binary content is
returned as a JSON object with a `base64` field. A call made under the original
fixed-version contract retains that recorded limit for historical browsing.

Use the existing versioned deployment for in-flight Studies created with the
earlier native tool contract. The new interface does not reconstruct their
original knowledge reads. For an offline acceptance run with a locally installed
Pi, see the test command in the native application README.
