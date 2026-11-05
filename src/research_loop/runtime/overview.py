"""Opt-in research summaries from accepted records, without acquiring ownership.

This audience may see research text. It never receives whole configurations,
contexts, raw responses, receipt bodies, artifact paths or access credentials.
Frontier history uses publication order and the canonical eligibility/Pareto
implementation; dependencies and recorded corrections are not scientific lineage.
"""

from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
import json
from threading import Lock
from types import SimpleNamespace

from ..evaluation import validate_evaluation
from .publication_views import build_views, render_research
from .store import StateStore, _decode
from .updates import Updater


_TEXT_FIELDS = (
    "title",
    "description",
    "instruction",
    "proposal",
    "hypothesis",
    "intervention",
)
_FRONTIERS = OrderedDict()
_CACHE_LOCK = Lock()


@lru_cache(maxsize=128)
def _accepted_output(root, reference, identity_items):
    reader = Updater(SimpleNamespace(store=SimpleNamespace(root=root)), validators={})
    return reader._read_record(reference, dict(identity_items))["normalized_output"]


def _read_output(reader, reference, identity):
    # Cache only immutable accepted normalized output. Metadata checks remain on
    # every request, including cache hits; raw envelopes are never retained.
    reader._check_identity(reference, identity)
    output = _accepted_output(reader._root, reference, tuple(sorted(identity.items())))
    reader._check_identity(reference, identity)
    return output


def _cached_frontier(store, study_id, snapshot, definition):
    key = (
        str(store.root),
        study_id,
        snapshot["version"],
        json.dumps(definition, sort_keys=True, allow_nan=False),
    )
    with _CACHE_LOCK:
        if key in _FRONTIERS:
            _FRONTIERS.move_to_end(key)
            return deepcopy(_FRONTIERS[key])
    value = _frontier(snapshot, definition)
    with _CACHE_LOCK:
        _FRONTIERS[key] = deepcopy(value)
        while len(_FRONTIERS) > 8:
            _FRONTIERS.popitem(last=False)
    return value


def _text_fields(value):
    return {
        key: value.get(key) if isinstance(value.get(key), str) else None
        for key in _TEXT_FIELDS
    }


def _feedback(definition):
    return (
        isinstance(definition, dict)
        and definition.get("data_role") == "development"
        and definition.get("allow_feedback") is True
    )


def _candidates(view, measurements, definition):
    names = {
        metric["name"]
        for kind in ("objectives", "constraints")
        for metric in definition[kind]
    }
    return [
        dict(
            measurement_id=row["id"],
            candidate_id=measurements[row["id"]]["candidate_id"],
            iteration_id=row["experiment_id"],
            metrics={key: row["metrics"][key] for key in sorted(names)},
        )
        for row in view["candidates"]
    ]


def _frontier(snapshot, definition):
    result = dict(
        status="unavailable",
        mode=None,
        points=[],
        current=[],
        excluded=[],
        message="No frozen global evaluation is configured.",
    )
    if definition is None:
        return None, result
    validate_evaluation(definition)
    evaluation = {
        key: definition[key]
        for key in ("id", "comparison_scope", "objectives", "constraints")
    }
    result["mode"] = (
        "single_objective" if len(definition["objectives"]) == 1 else "pareto"
    )
    if not _feedback(definition):
        result.update(
            status="feedback_hidden",
            message="Global evaluation does not permit development feedback.",
        )
        return evaluation, result
    visible = {
        row["id"]: row
        for row in snapshot["measurements"]
        if _feedback(row["definition"])
    }
    # Keep local scopes separate through the same canonical view builder. Only
    # the global view is projected; no local score becomes global evidence.
    state = dict(snapshot, measurements=list(visible.values()))
    state["eligibility"] = [
        row for row in snapshot["eligibility"] if row["measurement_id"] in visible
    ]
    view = None
    for publication in sorted(snapshot["publications"], key=lambda row: row["id"]):
        version = publication["id"]
        prefix = dict(state, version=version)
        prefix["publications"] = [
            row for row in state["publications"] if row["id"] <= version
        ]
        for key in ("knowledge", "eligibility", "conflicts"):
            prefix[key] = [
                row for row in state[key] if row["publication_id"] <= version
            ]
        view = build_views(prefix, definition)["global"][definition["id"]]
        candidates = _candidates(view, visible, definition)
        best = None
        if result["mode"] == "single_objective" and candidates:
            objective = definition["objectives"][0]
            choose = max if objective["direction"] == "maximize" else min
            best = choose(row["metrics"][objective["name"]] for row in candidates)
        result["points"].append(
            dict(publication_id=version, frontier=candidates, best_value=best)
        )
    if view is None:
        view = build_views(state, definition)["global"][definition["id"]]
    result.update(
        status="available",
        current=_candidates(view, visible, definition),
        excluded=[
            dict(measurement_id=row["id"], reason=row["reason"])
            for row in view["excluded"]
        ],
        message=(
            "Pareto frontier; no single best score is defined."
            if result["mode"] == "pareto"
            else "Reviewed global frontier by publication; corrections may lower the best value."
        ),
    )
    return evaluation, result


def _knowledge(snapshot, payloads):
    # Validate published metadata against immutable normalized payloads using the
    # canonical renderer. Its path-bearing Markdown is deliberately not returned.
    render_research(snapshot, payloads)
    visible = {
        row["id"] for row in snapshot["measurements"] if _feedback(row["definition"])
    }
    latest = {}
    eligibility = {}
    for row in snapshot["eligibility"]:
        old = eligibility.get(row["measurement_id"])
        if old is None or old["revision"] < row["revision"]:
            eligibility[row["measurement_id"]] = row
    for row in snapshot["knowledge"]:
        old = latest.get(row["id"])
        if old is None or old["revision"] < row["revision"]:
            latest[row["id"]] = row
    result = []
    for row in sorted(
        latest.values(), key=lambda row: (row["publication_id"], row["entry_index"])
    ):
        entry = payloads[row["publication_id"]]["knowledge"][row["entry_index"]]
        if not set(entry["measurement_ids"]) <= visible:
            continue
        item = {
            key: row[key]
            for key in ("id", "revision", "kind", "source_role", "publication_id")
        }
        item.update(
            deepcopy(
                {
                    key: entry[key]
                    for key in (
                        "statement",
                        "conditions",
                        "interpretation",
                        "limitations",
                        "measurement_ids",
                    )
                }
            )
        )
        item["source_count"] = len(entry["source_refs"])
        item["measurement_status"] = []
        for identity in entry["measurement_ids"]:
            status = eligibility.get(identity)
            item["measurement_status"].append(
                dict(
                    measurement_id=identity,
                    valid=bool(status["valid"]) if status else None,
                    verified=bool(status["verified"]) if status else None,
                    publication_id=status["publication_id"] if status else None,
                )
            )
        result.append(item)
    return result


def _budgets(config, rows):
    # Unused authorizations have not acquired a ledger row yet. Show their
    # configured limit and zero usage; durable ledger values win when present.
    balances = {}
    for identity, grant in config.get("operation_authorizations", {}).items():
        for unit, limit in grant.get("cost_limits", {}).items():
            balances[identity, unit] = dict(
                authorization_id=identity, unit=unit, limit=limit, held=0, settled=0
            )
    for row in rows:
        balances[row["authorization_id"], row["unit"]] = dict(
            authorization_id=row["authorization_id"],
            unit=row["unit"],
            limit=int(row["cost_limit"]),
            held=int(row["held"]),
            settled=int(row["settled"]),
        )
    for item in balances.values():
        item["remaining"] = item["limit"] - item["held"] - item["settled"]
    return [balances[key] for key in sorted(balances)]


def overview_snapshot(root):
    """Return an explicitly allowlisted research audience view, read-only.

    Database facts come from one SQLite read snapshot. Only referenced immutable
    accepted publication and Advocate artifacts are opened afterward, with the
    existing before/after identity checks. This does not create a coordinator,
    rebuild views, read evaluation receipts or probe arbitrary source paths.
    """
    store = StateStore(root)
    with store._connection(readonly=True) as conn:
        conn.execute("BEGIN")
        control = _decode(
            conn.execute("SELECT study_id,config_json FROM control").fetchone()
        )
        snapshot = {
            key: [
                _decode(row)
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for key, table in (
                ("publications", "publications"),
                ("knowledge", "knowledge"),
                ("eligibility", "eligibility"),
                ("conflicts", "publication_conflicts"),
                ("measurements", "measurements"),
            )
        }
        snapshot["version"] = max(
            (row["id"] for row in snapshot["publications"]), default=0
        )
        tasks = [
            _decode(row)
            for row in conn.execute(
                "SELECT t.*,r.origin_kind,r.origin_id,i.id AS iteration_id "
                "FROM tasks t JOIN task_revisions r ON r.id=t.id AND r.revision=t.revision "
                "LEFT JOIN iterations i ON i.task_id=t.id ORDER BY t.priority,t.id"
            )
        ]
        costs = [
            dict(row) for row in conn.execute("SELECT * FROM operation_scope_costs")
        ]
        planning_calls = [
            _decode(row)
            for row in conn.execute(
                "SELECT c.round_id,c.slot,a.id,a.role,a.status,a.output_ref,a.output_identity_json "
                "FROM planning_calls c JOIN attempts a ON a.id=c.attempt_id ORDER BY c.rowid"
            )
        ]
    # Updater's artifact reader only needs the store; no owner capability is
    # acquired or exposed by this read-only adapter.
    reader = Updater(SimpleNamespace(store=store), validators={})
    payloads = {
        row["id"]: _read_output(reader, row["payload_ref"], row["identity"])
        for row in snapshot["publications"]
    }
    config = control["config"]
    evaluation, frontier = _cached_frontier(
        store, control["study_id"], snapshot, config.get("global_evaluation")
    )
    knowledge = _knowledge(snapshot, payloads)
    call_rounds = {row["id"]: row["round_id"] for row in planning_calls}
    task_ids = {row["id"] for row in tasks}
    edges, projected_tasks = [], []
    for row in tasks:
        item = {
            key: row[key]
            for key in (
                "id",
                "revision",
                "source",
                "priority",
                "cancelled",
                "dependencies",
                "iteration_id",
                "origin_kind",
                "origin_id",
            )
        }
        item.update(_text_fields(row["specification"]))
        # Human origin identifiers are request-idempotency keys, not research
        # associations. Only Judge origins identify an accepted planning call.
        if row["origin_kind"] != "judge":
            item["origin_id"] = None
        item["round_id"] = (
            call_rounds.get(row["origin_id"]) if row["origin_kind"] == "judge" else None
        )
        projected_tasks.append(item)
        for source in row["dependencies"]:
            if source in task_ids:
                edges.append(
                    dict(source=source, target=row["id"], kind="task_dependency")
                )
        corrections = row["specification"].get("corrects_tasks", [])
        if isinstance(corrections, list):
            for source in corrections:
                if isinstance(source, str) and source in task_ids:
                    edges.append(
                        dict(source=source, target=row["id"], kind="task_correction")
                    )
    proposals = []
    for call in planning_calls:
        if call["role"] != "advocate" or call["status"] != "accepted":
            continue
        output = _read_output(reader, call["output_ref"], call["output_identity"])
        values = output.get("proposals", [])
        if not isinstance(values, list):
            raise ValueError("Malformed accepted Advocate proposals")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError("Malformed accepted Advocate proposal")
            specification = value.get("specification", {})
            if not isinstance(specification, dict):
                raise ValueError("Malformed proposal specification")
            proposals.append(
                dict(
                    id=value.get("id") if isinstance(value.get("id"), str) else None,
                    round_id=call["round_id"],
                    attempt_id=call["id"],
                    slot=call["slot"],
                    index=index,
                    reason=value.get("reason")
                    if isinstance(value.get("reason"), str)
                    else None,
                    priority=value.get("priority")
                    if type(value.get("priority")) is int
                    else None,
                    dependencies=[
                        identity
                        for identity in value.get("dependencies", [])
                        if isinstance(identity, str)
                    ]
                    if isinstance(value.get("dependencies", []), list)
                    else [],
                    **_text_fields(specification),
                )
            )
    return dict(
        study_id=control["study_id"],
        publication_version=snapshot["version"],
        goal=config.get("goal") if isinstance(config.get("goal"), str) else None,
        evaluation=evaluation,
        frontier=frontier,
        budgets=_budgets(config, costs),
        knowledge=knowledge,
        tasks=projected_tasks,
        proposals=proposals,
        graph=dict(
            nodes=[
                dict(id=row["id"], label=row["title"] or row["id"], kind="task")
                for row in projected_tasks
            ],
            edges=edges,
            message="Recorded task dependencies and corrections; scientific parentage is not inferred.",
        ),
    )
