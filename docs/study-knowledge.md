# Study knowledge and Pi role context

The native SQLite applications keep their role definitions in
[native roles](../integrations/native_runtime/roles/) and
[script roles](../integrations/script_runtime/roles/). Each definition names
the instruction file, controller input providers, library aliases, tools and
Pydantic output type. Execute receives its approved Plan. Review receives
that Plan, the accepted Execute and all measurements it must assess. Script
Plan and Execute also receive the frozen scientific contract. An invocation
captures the effective instructions, input sources, library revisions, tool
schemas and exact prompt in its immutable dispatch input.

## Configure and inspect a call

Choose read-only mounts at Study creation. Each path names a library root on
the application host and each version identifies a completed snapshot. The
main library has the reserved alias `study`. For example, this configuration
permits a later Plan to read the first published snapshot of a reference library.

```yaml
knowledge_libraries:
  - alias: literature
    path: /path/to/reference-study
    version: 1
    description: Versioned source excerpts
role_access:
  plan:
    knowledge_bases: [study, literature]
    tools: [read_index, search, read, submit_result]
role_definitions:
  plan:
    knowledge_bases: [study]
    tools: [read_index, search, read, submit_result]
```

To change subsequent Plan invocations, save this replacement override in a
private YAML file. Other fields come from the application's role definition.

```yaml
knowledge_bases: [study, literature]
tools: [read_index, read, submit_result]
```

```sh
uv run research-loop runtime role-config "$STUDY" plan --config plan-override.yaml
uv run research-loop runtime context "$STUDY" "$ATTEMPT"
uv run research-loop runtime context "$STUDY" "$ATTEMPT" --expand
uv run research-loop runtime knowledge-read "$STUDY" "$ATTEMPT" index.md
uv run research-loop runtime knowledge-read "$STUDY" "$ATTEMPT" finding --with-sources
uv run research-loop runtime knowledge-read "$STUDY" "$ATTEMPT" --alias "$ALIAS" --reference "$REF_JSON"
```

The context command reads the dispatch record for an attempt. Its default view
lists sources, library versions, tools and schema. Expansion includes the private
model-facing prompt and available trace directory. These commands require local
access to the Study. The role receives library descriptions and Index entry
points. It can read an Index, search locally, open a page or evidence record,
and cite the immutable receipt returned by a successful read. Indexes, search
snippets and reads enforce the transitive dependencies of the frozen revision.
Derived Evidence, pages and Indexes retain the disclosure restrictions of
consulted snippets, opened records and supplied operator inputs. A source
restricted to roles in its original Study cannot be made public by mounting
that library. The human historical read uses the role’s frozen grants.
The budget pays for the first model request on role reservation and for each
additional provider request before sending it. All turns share the call deadline. Set the effective Pi profile's
`retry.provider.maxRetries` to `0`. Set `transport` to `sse` for
`openai-codex-responses`. The extension checks these settings before HTTP
admission. The offline protocol tests use Pi 1.4.0 and a synthetic loopback
provider.

## Publish and maintain the library

A trusted program appends typed evidence with a fixed ID. Give external material
a versioned source and locator or preserve the cited excerpt. Repeating the same
publication is idempotent. Give a correction its own ID.

```python
from research_loop.runtime.knowledge import KnowledgeBase

library = KnowledgeBase(study_root)
receipt = library.publish_evidence(
    "E1", "experiment_receipt", {"outcome": "Synthetic observation"},
    metadata={"experiment_id": "example-1"},
)
```

After an accepted Scout or Review publication, the native coordinator ingests its
observations and provenance. Reviewed iterations can also contribute feedback-
permitted report descriptors, registered measurements and operation receipts.
The issuing iteration’s Review receives the raw script report. Shared
interpretations retain the report reference and its unscored provenance. Eligibility
decisions and Frontier calculations use their dedicated runtime ledgers.
Previously published entries retain their original IDs and revisions as
read-only historical Markdown.

Running the coordinator admits a Knowledge Manager when pending evidence or a
changed peer revision needs review and the Study has capacity. Consecutive
arrivals form a batch. It can read the main library and its permitted mounts,
stage page and Index edits, archive a page, or split and merge pages in one
submission. References carry a library ID, kind, stable record ID and revision.
The Index, affected pages, processed cursor and accepted result commit together
against the frozen base. A no-edit submission can acknowledge the batch. A
failed batch remains pending. An inaccessible evidence record stops processing
at its cursor until the Manager can read its dependencies. Run the following command to request a fresh
attempt under the same Study budgets.

```sh
uv run research-loop runtime maintain "$STUDY" --request-id retry-1
uv run research-loop runtime run "$STUDY"
```

Markdown revisions and their Index live under the Study's `knowledge/`
directory. SQLite binds the visible paths and revisions to snapshots. The
human read command above returns the same text as the role's tool. Earlier
calls can continue reading their original sources after newer commits.
