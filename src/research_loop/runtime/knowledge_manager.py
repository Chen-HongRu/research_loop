"""Bridge accepted publications into Evidence and coalesce main-library maintenance."""

from .call_context import dispatched_input
from .knowledge import KnowledgeBase
from .operations import Operations
from .source_scope import derived_dependencies, freeze_source_scope
from .store import _decode
from .updates import Updater


def request_maintenance(owner, resolve_role, *, retry_id=None):
    """A failed batch stays pending until explicit retry or another evidence arrival."""
    with owner.store._connection(readonly=True) as conn:
        active = conn.execute(
            "SELECT id FROM attempts WHERE role='knowledge_manager' AND status='active'"
        ).fetchone()
    if active:
        return {"attempt_id": active["id"], "state": "active"}
    library = KnowledgeBase(owner.store.root)
    status = library.status()
    start, end = status["processed_through"], status["evidence_through"]
    if start >= end and not status["needs_review"]:
        return {"state": "up_to_date"}
    config = owner.snapshot()["config"]
    aliases = resolve_role("knowledge_manager", config)["knowledge_bases"]
    scoped = library.freeze(config, aliases)
    library = KnowledgeBase(
        owner.store.root,
        config=config,
        source_scope=freeze_source_scope(library, config, "knowledge_manager", scoped),
    )
    batch = library.pending(manifest=scoped)
    if not batch["evidence"] and not batch["affected_pages"]:
        return {"state": "no_visible_work"}
    start, end = batch["processed_through"], batch["evidence_through"]
    key = f"runtime:maintenance:{status['version']}:{start}:{end}"
    if retry_id:
        key += ":" + retry_id
    attempt = owner.begin_attempt("knowledge_manager", request_id=key)
    return {"attempt_id": attempt["id"], "state": attempt["status"]}


def commit_maintenance(owner, conn, call, output):
    # The authority and the base come from the dispatched packet, independently
    # of the worker's result. Acceptance and publishing use one SQL transaction.
    packet = dispatched_input(owner.store, call.attempt_id)
    assembly = packet["assembly"]
    from .handoff_scope import require_bound_assembly

    require_bound_assembly(assembly)
    batch = assembly["inputs"]["maintenance"]
    minimum = batch["processed_through"] + (not batch["affected_pages"])
    if (
        assembly["role"] != "knowledge_manager"
        or not minimum <= output["processed_through"] <= batch["evidence_through"]
    ):
        raise ValueError(
            "Manager must advance within the frozen pending evidence batch"
        )
    library = KnowledgeBase(
        owner.store.root,
        config=packet["knowledge_config"],
        source_scope=assembly.get("source_scope", []),
    )
    # A native reader can paraphrase any material in its authorized directory.
    # Retain that scope even when outputs omit citations.
    disclosure = derived_dependencies(assembly, library.library_id)
    return library.commit(
        base_version=batch["base_version"],
        pages=[],
        index_markdown=None,
        files=output["files"],
        remove_paths=output.get("remove_paths", []),
        file_dependencies=disclosure,
        processed_through=output["processed_through"],
        request_id=call.attempt_id,
        manifest=assembly["maintenance_libraries"],
        conn=conn,
    )


class EvidenceIntake:
    """Replay the short publication suffix after interruption using immutable IDs."""

    def __init__(self, owner):
        self.owner = owner
        self.reader = Updater(owner, validators={})
        self.library = KnowledgeBase(owner.store.root)

    def sync(self):
        with self.owner._transaction() as (conn, _):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kb_intake (publication_id INTEGER PRIMARY KEY)"
            )
            cursor = conn.execute(
                "SELECT COALESCE(MAX(publication_id),0) FROM kb_intake"
            ).fetchone()[0]
            publications = [
                _decode(r)
                for r in conn.execute(
                    "SELECT p.*, a.role, a.iteration_id, d.input_ref, d.input_identity_json, d.adapter_json "
                    "FROM publications p JOIN attempts a ON a.id=p.attempt_id "
                    "LEFT JOIN dispatch_runs d ON d.attempt_id=a.id WHERE p.id>? ORDER BY p.id",
                    (cursor,),
                )
            ]
        for row in publications:
            self._ingest(row)
            with self.owner._transaction() as (conn, _):
                conn.execute("INSERT OR IGNORE INTO kb_intake VALUES(?)", (row["id"],))
        return len(publications)

    def _ingest(self, row):
        proposal = self.reader._read_record(row["payload_ref"], row["identity"])
        output = proposal["normalized_output"]
        version = row["id"]
        packet = (
            self.reader._read_record(row["input_ref"], row["input_identity"])
            if row["input_ref"]
            else {}
        )
        assembled = "assembly" in packet
        from .handoff_scope import require_bound_assembly

        if assembled:
            require_bound_assembly(packet["assembly"])
        elif (row.get("adapter") or {}).get("id") in {
            "native-runtime-v1",
            "native-script-runtime-v1",
        }:
            require_bound_assembly({})
        # Keep original published identities for calls made before the Markdown
        # library. A new role's observations are evidence for the Manager.
        if not assembled:
            with self.owner.store._connection(readonly=True) as conn:
                knowledge = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM knowledge WHERE publication_id=? ORDER BY entry_index",
                        (version,),
                    )
                ]
            self.library.import_legacy(
                {"version": version, "knowledge": knowledge}, {version: output}
            )
            return

        provenance = {
            "publication_id": version,
            "attempt_id": row["attempt_id"],
            "role": row["role"],
        }
        dependencies = derived_dependencies(packet["assembly"], self.library.library_id)
        for index, observation in enumerate(output["knowledge"]):
            self.library.publish_evidence(
                f"observation-{version}-{index}",
                "external" if observation["kind"] == "external" else row["role"],
                observation,
                source_refs=[
                    *dependencies,
                    row["payload_ref"],
                    *observation["source_refs"],
                ],
                metadata=provenance,
            )
        if row["role"] != "review":
            return
        with self.owner.store._connection(readonly=True) as conn:
            measurements = [
                _decode(r)
                for r in conn.execute(
                    "SELECT * FROM measurements WHERE iteration_id=? ORDER BY id",
                    (row["iteration_id"],),
                )
            ]
        for i, item in enumerate(measurements):
            self.library.publish_evidence(
                f"measurement-{version}-{i}",
                "measurement",
                self.reader._read_record(item["receipt_ref"], item["identity"]),
                source_refs=[*dependencies, item["receipt_ref"]],
                metadata={"measurement_id": item["id"], **provenance},
            )
        for i, decision in enumerate(output["reviews"] + output["corrections"]):
            self.library.publish_evidence(
                f"assessment-{version}-{i}",
                "review",
                decision,
                source_refs=[*dependencies, row["payload_ref"]],
                metadata=provenance,
            )
        operations = Operations(self.owner)
        for i, op in enumerate(operations.list(iteration_id=row["iteration_id"])):
            if op["collection_ref"]:
                self.library.publish_evidence(
                    f"operation-{version}-{i}",
                    "experiment_receipt",
                    {
                        key: op[key]
                        for key in (
                            "id",
                            "state",
                            "terminal_outcome",
                            "collection_state",
                        )
                    },
                    source_refs=[*dependencies, op["collection_ref"]],
                    metadata=provenance,
                )
        reports, _ = operations.research_evidence(row["iteration_id"])
        for i, report in enumerate(reports):
            source = report["source"]
            if source["allow_feedback"]:
                self.library.publish_evidence(
                    f"report-{version}-{i}",
                    "run_report",
                    # Only the issuing iteration's Review may receive the raw
                    # observation. The shared library can identify its receipt.
                    {
                        "report": source["id"],
                        "classification": "unscored_research_report",
                    },
                    source_refs=[*dependencies, source["ref"]],
                    metadata=provenance | {"source": source["revision"]},
                )
