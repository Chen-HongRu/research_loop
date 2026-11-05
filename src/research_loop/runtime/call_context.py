"""Inspect the exact role assembly bound to an immutable dispatch input."""

import json

from .store import ConflictError, _decode
from .updates import _identity


def dispatched_input(store, attempt_id):
    with store._connection(readonly=True) as conn:
        run = _decode(
            conn.execute(
                "SELECT input_ref,input_identity_json FROM dispatch_runs WHERE category='call' AND attempt_id=?",
                (attempt_id,),
            ).fetchone()
        )
    if run is None:
        raise ValueError("This attempt has no dispatched context")
    ref = run["input_ref"]
    if _identity(store.root, ref) != run["input_identity"]:
        raise ConflictError("Dispatched input identity changed")
    return json.loads((store.root / ref).read_bytes())


def preview(store, attempt_id, *, expand=False):
    from .role_assembly import preview_assembly

    packet = dispatched_input(store, attempt_id)
    if "assembly" not in packet:
        raise ValueError(
            "This call predates role assembly or uses a custom application"
        )
    result = preview_assembly(packet["assembly"], expand=expand)
    with store._connection(readonly=True) as conn:
        endpoint_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='invocation_knowledge'"
        ).fetchone()
        endpoints = (
            conn.execute(
                "SELECT start_version,end_version FROM invocation_knowledge WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if endpoint_table
            else None
        )
        accepted = _decode(
            conn.execute(
                "SELECT a.iteration_id,i.task_id,d.token AS invocation_id,a.output_ref,"
                "a.output_identity_json,a.receipt_json FROM attempts a "
                "JOIN dispatch_runs d ON d.attempt_id=a.id AND d.category='call' "
                "LEFT JOIN iterations i ON i.id=a.iteration_id WHERE a.id=?",
                (attempt_id,),
            ).fetchone()
        )
    receipt = accepted["receipt"] or {}
    result.update(
        attempt_id=attempt_id,
        invocation_id=accepted["invocation_id"],
        iteration_id=accepted["iteration_id"],
        task_id=accepted["task_id"],
    )
    result["knowledge_endpoints"] = dict(
        start_version=endpoints[0] if endpoints else None,
        end_version=endpoints[1] if endpoints else None,
        meaning="Published versions at invocation start and end; not a reading trace. Missing values were not recorded",
    )
    from .knowledge import KnowledgeBase

    result["knowledge_publication"] = KnowledgeBase(store.root).live_status()
    result["accepted_output_ref"] = accepted["output_ref"]
    result["decision_refs"] = receipt.get("evidence_refs")
    if expand and accepted["output_ref"] is not None:
        ref = accepted["output_ref"]
        if _identity(store.root, ref) != accepted["output_identity"]:
            raise ConflictError("Accepted output identity changed")
        result["accepted_output"] = json.loads((store.root / ref).read_bytes())[
            "normalized_output"
        ]
    from .model_requests import call_trace

    trace = call_trace(store.root, attempt_id)
    if trace is not None:
        result["trace_available"] = True
        if expand:
            result["private_trace_directory"] = str(trace)
    return result


def read_at_call(store, attempt_id, alias, page, *, with_sources=False, version=None):
    """Browse a fixed revision/version within the historical role's authority."""
    from .knowledge import KnowledgeBase

    packet = dispatched_input(store, attempt_id)
    assembly = packet.get("assembly")
    if not isinstance(assembly, dict):
        raise ValueError("This call predates recorded role knowledge authority")
    aliases = [entry["alias"] for entry in assembly["knowledge_libraries"]]
    if alias not in aliases:
        raise ValueError("This call did not authorize the requested library")
    library = KnowledgeBase(
        store.root,
        config=packet["knowledge_config"],
        source_scope=assembly.get("source_scope", []),
    )
    if assembly["version"] == 1:
        # The original call recorded its exact library and tool ceilings.
        manifest = assembly["knowledge_libraries"]
        required = {"read_index"} if page == "index.md" else {"read"}
        if isinstance(page, dict) and page.get("kind") == "index":
            required.add("read_index")
        if not required.issubset(assembly["tools"]):
            raise PermissionError("The historical call did not grant this read tool")
        recorded = next(entry for entry in manifest if entry["alias"] == alias)
        if version is not None and version != recorded["version"]:
            raise ValueError("This historical call bound a different library version")
    else:
        if "read" not in assembly["tools"]:
            raise PermissionError("The historical call did not grant the read tool")
        if version is None and not isinstance(page, dict):
            raise ValueError(
                "Choose --version or an exact --reference. Invocation endpoints do not identify what was read"
            )
        library.check_bindings(assembly["knowledge_libraries"], aliases=aliases)
        manifest = (
            library.freeze(aliases=aliases)
            if version is None
            else library.history(version, aliases=aliases, alias=alias)
        )
    record = library.read_with_sources(manifest, alias, page)
    if with_sources:
        return record | {
            "history_note": "Fixed historical content; not evidence that this invocation read it"
        }
    return record["content"]
