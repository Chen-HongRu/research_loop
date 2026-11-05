"""Registered retrieval and isolated Manager draft tools for frozen role grants."""

from copy import deepcopy

from research_loop.runtime.operation_contract import fields, json_value

from .policy import DraftPage


def _tool(description, properties, required):
    return dict(
        description=description,
        parameters=dict(
            type="object",
            properties=properties,
            required=required,
            additionalProperties=False,
        ),
    )


STRING = {"type": "string", "minLength": 1}
TOOLS = {
    "read_index": _tool(
        "Read the authorized library Index at the frozen snapshot.",
        {"alias": STRING},
        ["alias"],
    ),
    "search": _tool(
        "Search only authorized frozen library content.",
        {"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ["query"],
    ),
    "read": _tool(
        "Read an authorized page, evidence record or fixed reference at the frozen snapshot.",
        {"alias": STRING, "path": {"anyOf": [STRING, {"type": "object"}]}},
        ["alias", "path"],
    ),
    "submit_result": _tool(
        "Submit the complete final response and end the role call.", {}, []
    ),
    "edit_page": {
        "description": "Stage one main-library page edit for final submission.",
        "parameters": DraftPage.model_json_schema(),
    },
    "edit_index": _tool(
        "Stage the main-library Markdown Index. Changes remain private until accepted submit_result.",
        {"markdown": {"type": "string"}},
        ["markdown"],
    ),
}


def handler(assembly, *, study_root, config):
    """Each closure owns a private draft; no tool mutates the published library."""
    from research_loop.runtime.knowledge import KnowledgeBase

    kb = KnowledgeBase(
        study_root, config=config, source_scope=assembly.get("source_scope", [])
    )
    manifests = assembly["knowledge_libraries"]
    draft = {"pages": [], "index_markdown": None}

    def invoke(name, args):
        if name not in assembly["tools"] or name == "submit_result":
            raise ValueError("Tool is outside the frozen role grant")
        args = json_value(args)
        if name == "read_index":
            fields(args, {"alias"}, name)
            return {
                **kb.index_with_sources(manifests, args["alias"]),
                "snapshot": manifests,
                "alias": args["alias"],
            }
        if name == "search":
            fields(args, {"query"} | ({"limit"} & args.keys()), name)
            return kb.search(manifests, args["query"], limit=args.get("limit", 10))
        if name == "read":
            fields(args, {"alias", "path"}, name)
            is_index = args["path"] == "index.md" or (
                isinstance(args["path"], dict) and args["path"].get("kind") == "index"
            )
            if is_index and "read_index" not in assembly["tools"]:
                raise ValueError("Index reads require the frozen read_index grant")
            return {
                **kb.read_with_sources(manifests, args["alias"], args["path"]),
                "alias": args["alias"],
                "snapshot": manifests,
            }
        if assembly["role"] != "knowledge_manager":
            raise ValueError("Draft editing requires Knowledge Manager")
        if name == "edit_page":
            fields(args, {"page"}, name)
            page = args["page"]
            if not isinstance(page, dict) or not isinstance(page.get("id"), str):
                raise ValueError("Draft page requires a stable ID")
            draft["pages"] = [p for p in draft["pages"] if p["id"] != page["id"]] + [
                deepcopy(page)
            ]
        elif name == "edit_index":
            fields(args, {"markdown"}, name)
            if not isinstance(args["markdown"], str):
                raise ValueError("Draft Index must be Markdown text")
            draft["index_markdown"] = args["markdown"]
        else:
            raise ValueError("Unknown registered tool")
        return {"staged": True, "published": False}

    return invoke, draft
