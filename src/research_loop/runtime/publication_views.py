"""Pure derivative views of published research records, without artifact I/O.

Eligibility revisions change membership, never measurement values. Text is copied
from accepted entries with attribution and limitations; projection is not proof.
"""

from collections.abc import Mapping
from copy import deepcopy
import json

from ..evaluation import build_frontier, validate_evaluation


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _rows(snapshot, key):
    value = snapshot.get(key)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"Invalid snapshot {key}")
    return value


def _unique(rows, key):
    result = {}
    for row in rows:
        identity = row.get(key)
        if not _text(identity) or identity in result:
            raise ValueError(f"Invalid or duplicate {key}")
        result[identity] = row
    return result


def _snapshot(snapshot):
    if not isinstance(snapshot, dict) or not _integer(snapshot.get("version")):
        raise ValueError("Invalid publication snapshot version")
    publications = {}
    for row in _rows(snapshot, "publications"):
        identity = row.get("id")
        if not _integer(identity, 1) or identity in publications:
            raise ValueError("Invalid or duplicate publication id")
        publications[identity] = row
    if snapshot["version"] != max(publications, default=0):
        raise ValueError("Snapshot version differs from publication references")
    measurements = _unique(_rows(snapshot, "measurements"), "id")
    for kind, identity in (("knowledge", "id"), ("eligibility", "measurement_id")):
        seen = set()
        for row in _rows(snapshot, kind):
            key, revision = row.get(identity), row.get("revision")
            if (
                not _text(key)
                or not _integer(revision, 1)
                or (key, revision) in seen
                or row.get("publication_id") not in publications
            ):
                raise ValueError(f"Invalid {kind} revision or publication reference")
            seen.add((key, revision))
            if kind == "knowledge":
                if (
                    row.get("kind")
                    not in {"external", "local_measurement", "interpretation"}
                    or row.get("source_role") not in {"scout", "review"}
                    or not _integer(row.get("entry_index"))
                ):
                    raise ValueError("Invalid knowledge metadata")
            else:
                if (
                    key not in measurements
                    or row.get("decision_kind") not in {"reviews", "corrections"}
                    or not _integer(row.get("decision_index"))
                    or any(
                        type(row.get(name)) is not int or row[name] not in (0, 1)
                        for name in ("valid", "verified")
                    )
                ):
                    raise ValueError("Invalid eligibility metadata")
    seen_conflicts = set()
    for row in _rows(snapshot, "conflicts"):
        if (
            not _integer(row.get("id"), 1)
            or row["id"] in seen_conflicts
            or row.get("publication_id") not in publications
            or row.get("target_kind") not in {"knowledge", "reviews", "corrections"}
            or not _text(row.get("target_id"))
            or not _text(row.get("reason"))
            or not _integer(row.get("expected_revision"))
            or not _integer(row.get("actual_revision"))
            or not _integer(row.get("entry_index"))
        ):
            raise ValueError("Invalid conflict metadata")
        seen_conflicts.add(row["id"])
    return publications, measurements


def _latest(rows, identity):
    result = {}
    for row in rows:
        key = row[identity]
        if key not in result or row["revision"] > result[key]["revision"]:
            result[key] = row
    return result


def build_views(snapshot: dict, global_evaluation: dict) -> dict:
    """Rebuild separated global/local Frontiers from all reviewed measurements."""
    _, measurements = _snapshot(snapshot)
    validate_evaluation(global_evaluation)
    groups = {(None, global_evaluation["id"]): (global_evaluation, [])}
    eligibility = _latest(snapshot["eligibility"], "measurement_id")
    for identity in sorted(measurements):
        if identity not in eligibility:
            continue
        row = measurements[identity]
        definition = row.get("definition")
        if not isinstance(definition, dict):
            raise ValueError("Measurement requires its frozen evaluation definition")
        validate_evaluation(definition)
        evaluation_id = row.get("evaluation_id")
        if evaluation_id != definition["id"]:
            raise ValueError("Measurement evaluation id differs from definition")
        if row.get("level") == "global":
            if definition != global_evaluation:
                raise ValueError(
                    "Global measurement differs from fixed global evaluation"
                )
            key = (None, evaluation_id)
        elif row.get("level") == "local":
            if evaluation_id == global_evaluation["id"]:
                raise ValueError("Local evaluation cannot own the global id")
            if not _text(row.get("route_id")):
                raise ValueError("Local measurement requires a route id")
            key = (row["route_id"], evaluation_id)
        else:
            raise ValueError("Unknown measurement level")
        existing_definition, candidates = groups.setdefault(key, (definition, []))
        if existing_definition != definition:
            raise ValueError("Conflicting definitions within a route/evaluation key")
        status = eligibility[identity]
        for field in ("iteration_id", "receipt_ref"):
            if not _text(row.get(field)):
                raise ValueError(f"Measurement requires {field}")
        if not isinstance(row.get("artifact"), dict) or not isinstance(
            row.get("metrics"), dict
        ):
            raise ValueError("Measurement requires artifact and metrics")
        candidates.append(
            dict(
                id=identity,
                experiment_id=row["iteration_id"],
                artifact=row["artifact"],
                evaluation_id=evaluation_id,
                comparison_scope=definition["comparison_scope"],
                metrics=row["metrics"],
                evidence=[row["receipt_ref"]],
                valid=bool(status["valid"]),
                verified=bool(status["verified"]),
            )
        )
    result = {"version": snapshot["version"], "global": {}, "local": {}}
    for (route, evaluation_id), (definition, candidates) in sorted(
        groups.items(),
        key=lambda item: (item[0][0] is not None, item[0][0] or "", item[0][1]),
    ):
        view = build_frontier(definition, candidates)
        if route is None:
            result["global"][evaluation_id] = view
        else:
            result["local"].setdefault(route, {})[evaluation_id] = view
    return result


def _entry(value, row, measurements):
    fields = {
        "id",
        "expected_revision",
        "kind",
        "statement",
        "conditions",
        "source_refs",
        "measurement_ids",
        "interpretation",
        "limitations",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Malformed accepted knowledge entry")
    if (
        value["id"] != row["id"]
        or value["kind"] != row["kind"]
        or type(value["expected_revision"]) is not int
        or value["expected_revision"] != row["revision"] - 1
        or not _text(value["statement"])
        or not isinstance(value["interpretation"], str)
    ):
        raise ValueError("Knowledge entry differs from publication metadata")
    for name in ("conditions", "source_refs", "measurement_ids", "limitations"):
        if not isinstance(value[name], list) or not all(map(_text, value[name])):
            raise ValueError(f"Invalid knowledge {name}")
    if not set(value["measurement_ids"]) <= measurements.keys():
        raise ValueError("Knowledge references an unknown measurement")
    if not value["conditions"] or not value["limitations"]:
        raise ValueError("Knowledge requires explicit conditions and limitations")
    if value["kind"] == "external" and not value["source_refs"]:
        raise ValueError("External knowledge requires source attribution")
    if value["kind"] == "local_measurement" and (
        not value["measurement_ids"] or row["source_role"] != "review"
    ):
        raise ValueError("Local measurement knowledge requires Review measurement ids")
    return value


def render_research(snapshot: dict, publications: Mapping[int, dict]) -> str:
    """Render latest accepted entry revisions, preserving attribution and caveats.

    ``publications`` maps publication ids to their immutable normalized_output
    batches, loaded by the controller. This function performs no file access.
    """
    refs, measurements = _snapshot(snapshot)
    if not isinstance(publications, Mapping) or set(publications) != refs.keys():
        raise ValueError("Publication payloads must match snapshot references")
    for identity, batch in publications.items():
        if (
            type(identity) is not int
            or not isinstance(batch, dict)
            or set(batch) != {"knowledge", "reviews", "corrections"}
        ):
            raise ValueError("Malformed publication payload")
        for field in ("knowledge", "reviews", "corrections"):
            if not isinstance(batch.get(field), list):
                raise ValueError("Malformed normalized publication batch")
    entries = {}
    for row in snapshot["knowledge"]:
        batch = publications[row["publication_id"]]["knowledge"]
        if row["entry_index"] >= len(batch):
            raise ValueError("Knowledge index is outside its published batch")
        entries[(row["id"], row["revision"])] = _entry(
            batch[row["entry_index"]], row, measurements
        )
    for row in snapshot["eligibility"]:
        batch = publications[row["publication_id"]][row["decision_kind"]]
        if row["decision_index"] >= len(batch) or not isinstance(
            batch[row["decision_index"]], dict
        ):
            raise ValueError("Eligibility index is outside its published batch")
        decision = batch[row["decision_index"]]
        fields = {"measurement_id", "valid", "verified", "reason"}
        if row["decision_kind"] == "corrections":
            fields |= {"expected_revision", "evidence_refs"}
            refs = decision.get("evidence_refs")
            if (
                type(decision.get("expected_revision")) is not int
                or decision["expected_revision"] != row["revision"] - 1
                or row["revision"] < 2
                or not isinstance(refs, list)
                or not all(map(_text, refs))
            ):
                raise ValueError("Malformed published correction")
        elif row["revision"] != 1:
            raise ValueError("Initial Review must have eligibility revision one")
        if (
            set(decision) != fields
            or decision.get("measurement_id") != row["measurement_id"]
            or not _text(decision.get("reason"))
            or any(
                type(decision.get(name)) is not bool or int(decision[name]) != row[name]
                for name in ("valid", "verified")
            )
        ):
            raise ValueError("Eligibility decision differs from publication metadata")
    for row in snapshot["conflicts"]:
        conflict_entries = publications[row["publication_id"]][row["target_kind"]]
        index = row["entry_index"]
        if index >= len(conflict_entries) or not isinstance(
            conflict_entries[index], dict
        ):
            raise ValueError("Conflict index is outside its published batch")
        key = "id" if row["target_kind"] == "knowledge" else "measurement_id"
        expected = (
            0
            if row["target_kind"] == "reviews"
            else conflict_entries[index].get("expected_revision")
        )
        if (
            conflict_entries[index].get(key) != row["target_id"]
            or expected != row["expected_revision"]
        ):
            raise ValueError("Conflict target differs from its published batch")
    lines = ["# Research", "", f"Publication version: {snapshot['version']}", ""]
    latest = _latest(snapshot["knowledge"], "id")
    cited_measurements = set()
    for row in sorted(
        latest.values(),
        key=lambda item: (item["publication_id"], item["entry_index"], item["id"]),
    ):
        entry = entries[(row["id"], row["revision"])]
        cited_measurements.update(entry["measurement_ids"])
        lines.extend(
            [
                f"## {row['id']}",
                "",
                f"Revision: {row['revision']}; previous revision: {entry['expected_revision']}; "
                f"publication: {row['publication_id']}; source role: {row['source_role']}; kind: {row['kind']}",
                "",
            ]
        )
        if row["kind"] == "external":
            lines.extend(["Attribution: external evidence.", ""])
        elif row["kind"] == "interpretation":
            lines.extend(["Interpretation status: unverified.", ""])
        else:
            lines.extend(["Attribution: local measurement records.", ""])
        lines.extend([entry["statement"], ""])
        for title, field in (
            ("Conditions", "conditions"),
            ("Sources", "source_refs"),
            (
                "Measurement IDs (current status: [Measurement eligibility](#measurement-eligibility))",
                "measurement_ids",
            ),
        ):
            lines.append(f"{title}:")
            lines.extend(f"- {value}" for value in entry[field])
            if not entry[field]:
                lines.append("- None recorded.")
            lines.append("")
        if entry["interpretation"]:
            lines.extend(
                ["Interpretation (unverified):", "", entry["interpretation"], ""]
            )
        lines.append("Limitations:")
        lines.extend(f"- {value}" for value in entry["limitations"])
        if not entry["limitations"]:
            lines.append("- None recorded.")
        lines.append("")
    lines.extend(["## Measurement eligibility", ""])
    current_eligibility = _latest(snapshot["eligibility"], "measurement_id")
    for measurement_id in sorted(current_eligibility.keys() | cited_measurements):
        lines.extend([f"### {measurement_id}", ""])
        if measurement_id not in current_eligibility:
            lines.extend(["No published eligibility recorded.", ""])
            continue
        row = current_eligibility[measurement_id]
        decision = publications[row["publication_id"]][row["decision_kind"]][
            row["decision_index"]
        ]
        lines.extend(
            [
                f"Valid: {str(bool(row['valid'])).lower()}; verified: {str(bool(row['verified'])).lower()}",
                "",
                f"Decision: {row['decision_kind']}; eligibility revision: {row['revision']}; "
                f"publication: {row['publication_id']}",
                "",
                "Reason:",
                "",
                decision["reason"],
                "",
            ]
        )
    if not current_eligibility and not cited_measurements:
        lines.extend(["None recorded.", ""])
    lines.extend(["## Pending publication conflicts", ""])
    for row in sorted(snapshot["conflicts"], key=lambda item: item["id"]):
        lines.extend(
            [
                "```json",
                json.dumps(
                    deepcopy(row), ensure_ascii=False, sort_keys=True, allow_nan=False
                ),
                "```",
                "",
            ]
        )
    if not snapshot["conflicts"]:
        lines.extend(["None recorded.", ""])
    return "\n".join(lines)
