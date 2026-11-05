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
    from .model_requests import call_trace

    trace = call_trace(store.root, attempt_id)
    if trace is not None:
        result["trace_available"] = True
        if expand:
            result["private_trace_directory"] = str(trace)
    return result


def read_at_call(store, attempt_id, alias, page, *, with_sources=False):
    """Open the same Markdown or evidence that this dispatched role could read."""
    from .knowledge import KnowledgeBase

    packet = dispatched_input(store, attempt_id)
    assembly = packet.get("assembly")
    tools = assembly["tools"] if assembly is not None else {}
    required = {"read_index"} if page == "index.md" else {"read"}
    if isinstance(page, dict) and page.get("kind") == "index":
        required.add("read_index")
    if not required.issubset(tools):
        raise ValueError(
            f"This call lacks required tools: {sorted(required - tools.keys())}"
        )
    library = KnowledgeBase(
        store.root,
        config=packet["knowledge_config"],
        source_scope=assembly.get("source_scope", []),
    )
    record = library.read_with_sources(assembly["knowledge_libraries"], alias, page)
    return record if with_sources else record["content"]
