"""Deterministic recovery of small, already published Exp evidence.

This module never executes an experiment or reads scientific input contents. A
recovered evaluation is authoritative only when its original strict Critic result
and the immutable published assessment agree, apart from host publication help.
"""

from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

from research_workflows.programs.exp import EVALUATE_SCHEMA, _reference
from research_loop.state import write_json
from research_workflows.contracts import validate_stage

_LIMIT = 4 * 1024 * 1024
_TOTAL_LIMIT = 16 * 1024 * 1024


def _small(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
        raise ValueError("Recovery requires a small regular evidence file")
    with path.open("rb") as stream:
        content = stream.read(_LIMIT + 1)
    if len(content) > _LIMIT:
        raise ValueError("Recovery evidence exceeded the small-file limit")
    return content


def _json(path):
    return json.loads(_small(path))


def _strict_evaluation(value):
    if not isinstance(value, dict) or set(value) != set(EVALUATE_SCHEMA["required"]):
        return False
    try:
        validate_stage(value, "Recovered evaluation")
    except ValueError:
        return False
    for field, definition in EVALUATE_SCHEMA["properties"].items():
        item = value[field]
        if definition["type"] == "string":
            if not isinstance(item, str):
                return False
        elif not isinstance(item, list) or any(not isinstance(x, str) for x in item):
            return False
    return True


def _published(run_root, run):
    """Read boundary metadata; never discover or scan research input directories."""
    found = []
    for number in range(1, run["round"] + 1):
        directory = run_root / "rounds" / f"{number:06d}"
        mapping = _json(directory / "artifacts.json")
        output = _json(directory / "output.json")
        for source, ref in mapping.items():
            relative = Path(source)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError("Invalid published ownwork identity")
            path = run_root / "exports" / f"{number:06d}" / relative
            if str(path) != ref or ref not in output["artifacts"]:
                raise ValueError("Evidence must be an explicitly published export")
            cursor = run_root
            for part in path.relative_to(run_root).parts:
                cursor /= part
                if cursor.is_symlink():
                    raise ValueError("Published evidence may not contain symlinks")
            found.append({"source": source, "path": path, "round": number})
    if len(found) > 128:
        raise ValueError("Recovery boundary metadata exceeds the bounded file count")
    return found


def _original_call(state, exported):
    matches = []
    for index, call in enumerate(state.get("native_calls", [])):
        result = call.get("result")
        if (
            call.get("binding") == "evaluate"
            and call.get("status") == "completed"
            and _strict_evaluation(result)
            and all(result[key] == exported[key] for key in result if key != "help")
        ):
            matches.append((index, call))
    if len(matches) != 1:
        raise ValueError(
            "Original assessment lacks a unique matching strict Critic result"
        )
    index, call = matches[0]
    if call["result"]["status"] != "ready":
        raise ValueError(
            "Original Critic requested scientific help; it cannot be cleared"
        )
    thread_id = call.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("Original Critic context identity is unavailable")
    if any(
        other.get("thread_id") == thread_id and other.get("binding") != "evaluate"
        for other in state.get("native_calls", [])
    ):
        raise ValueError("Original Critic shares a scientific author context")
    return deepcopy(call["result"]), {
        "raw_call_index": index,
        "context": call["context"],
        "thread_id": thread_id,
        "agent_id": call.get("agent_id"),
        "session_id": call.get("session_id"),
    }


def _scope_citation(work, ref, roots):
    """Check a fixed readonly-scope citation using file metadata only."""
    candidate = Path(ref)
    candidate = candidate if candidate.is_absolute() else work / candidate
    resolved = candidate.resolve(strict=True)
    lexical = Path(os.path.abspath(candidate))
    for root in roots:
        root = Path(root)
        if not resolved.is_relative_to(root) or not lexical.is_relative_to(root):
            continue
        cursor = root
        for part in lexical.relative_to(root).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("Input-scope citations may not contain symlinks")
        if root.is_dir() and resolved.is_file():
            return str(resolved)
    raise ValueError(
        "Assessment citation is outside the declared readonly input scopes"
    )


def build_recovery_bundle(run_root, destination):
    """Copy published reports and return ``(manifest_path, manifest)``.

    Authority failures produce warnings and a null evaluation_ref. Consumers must
    stop there; a copied receipt or a nonempty assessment is not a success proof.
    Destinations must be new, avoiding overwrite of evidence or prior bundles.
    """
    run_root = Path(run_root).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(run_root):
        raise ValueError("Recovery destination must be new and outside the source Run")
    run = _json(run_root / "run.json")
    if (
        run.get("group_id") != "exp"
        or run.get("run_id") != run_root.name
        or not isinstance(run.get("round"), int)
        or run["round"] < 1
    ):
        raise ValueError("Recovery requires an identified Exp Run")
    published = _published(run_root, run)
    latest = {entry["source"]: entry for entry in published}
    evaluations = [entry for entry in published if entry["source"] == "evaluate.json"]
    chosen = list(latest.values())
    for entry in evaluations[:-1]:
        if entry not in chosen:
            chosen.append(entry)
    chosen.sort(key=lambda entry: (entry["round"], entry["source"]))
    destination.mkdir(parents=True)
    manifest = {
        "version": 1,
        "source_run": {
            "run_id": run["run_id"],
            "group_id": run["group_id"],
            "version": run.get("version"),
            "closed": run.get("status") == "closed",
            "round": run["round"],
        },
        "artifacts": [],
        "execution_ref": None,
        "evaluation_ref": None,
        "original_evaluation_ref": None,
        "recovery_evaluation_ref": None,
        "report_ref": None,
        "metrics_ref": None,
        "warnings": [],
        "limitations": [],
        "input_scope_roots": [],
    }
    copied, contents = {}, {}
    total = 0
    for entry in chosen:
        content = _small(entry["path"])
        total += len(content)
        if total > _TOTAL_LIMIT:
            raise ValueError("Recovery exports exceed the bounded total size")
        target = destination / "exports" / f"{entry['round']:06d}" / entry["source"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        copied[(entry["round"], entry["source"])] = str(target)
        contents[(entry["round"], entry["source"])] = content
        manifest["artifacts"].append(
            {
                "kind": entry["source"],
                "ref": str(target),
                "source_ref": str(entry["path"]),
                "source_round": entry["round"],
                "size": len(content),
            }
        )

    def latest_ref(source):
        entry = latest.get(source)
        return copied[(entry["round"], source)] if entry else None

    manifest["execution_ref"] = latest_ref("execution.json")
    manifest["report_ref"] = latest_ref("report.md") or latest_ref("report.json")
    if evaluations:
        first, last = evaluations[0], evaluations[-1]
        manifest["original_evaluation_ref"] = copied[(first["round"], "evaluate.json")]
        if len(evaluations) > 1:
            manifest["recovery_evaluation_ref"] = copied[
                (last["round"], "evaluate.json")
            ]
    try:
        if not manifest["source_run"]["closed"]:
            raise ValueError("Source Run is not closed")
        prepare_entry = latest["prepare.json"]
        prepare = json.loads(contents[(prepare_entry["round"], "prepare.json")])
        result_file, citation = _reference(
            SimpleNamespace(work=run_root / "work"),
            prepare["result_file"],
            existing=False,
        )
        if citation or result_file not in latest:
            raise ValueError("Prepared result is not a published evidence artifact")
        manifest["metrics_ref"] = latest_ref(result_file)
        execution_entry = latest["execution.json"]
        execution = json.loads(contents[(execution_entry["round"], "execution.json")])
        operation = _json(run_root / "executions.json").get("prepared-experiment", {})
        receipt = operation.get("receipt", {})
        if (
            operation.get("status") != "completed"
            or not isinstance(receipt, dict)
            or {key: value for key, value in execution.items() if key != "backend"}
            != receipt
            or receipt.get("exit_code") != 0
            or receipt.get("running")
            or receipt.get("unknown")
            or receipt.get("error")
        ):
            raise ValueError(
                "Prepared execution lacks matching settled successful receipts"
            )
        if not evaluations:
            raise ValueError("Original independent evaluation is unavailable")
        original = json.loads(contents[(first["round"], "evaluate.json")])
        if not _strict_evaluation(original):
            raise ValueError(
                "Original exported evaluation does not match the strict schema"
            )
        state = _json(run_root / "state.json")
        raw, provenance = _original_call(state, original)
        fixed_input = state.get("research_context", {}).get("input", {})
        scope_roots = list(
            dict.fromkeys(
                str(Path(fixed_input[field]).resolve())
                for field in ("project_root", "published_knowledge_root")
                if isinstance(fixed_input.get(field), str)
                and Path(fixed_input[field]).is_absolute()
            )
        )
        manifest["input_scope_roots"] = scope_roots
        admitted = [
            run.get("task_ref"),
            *run.get("input_refs", []),
            *run.get("feedback_refs", []),
        ]
        for number in range(1, run["round"] + 1):
            incoming = _json(run_root / "rounds" / f"{number:06d}" / "input.json")
            admitted.extend(
                [
                    incoming.get("task_ref"),
                    *incoming.get("input_refs", []),
                    *incoming.get("feedback_refs", []),
                ]
            )
        normalized = deepcopy(raw)
        input_citations, changes = [], []
        for field in ("evidence", "artifacts"):
            refs = []
            for index, ref in enumerate(raw[field]):
                try:
                    relative, citation = _reference(
                        SimpleNamespace(work=run_root / "work"),
                        ref,
                        admitted,
                        existing=False,
                    )
                except ValueError:
                    if field != "evidence":
                        raise
                    relative = _scope_citation(run_root / "work", ref, scope_roots)
                    citation = True
                if citation:
                    input_citations.append(relative)
                    if field == "artifacts":
                        continue
                    target = relative
                elif relative in latest:
                    target = latest_ref(relative)
                    source_round = latest[relative]["round"]
                    if source_round > first["round"]:
                        limitation = {
                            "kind": "later-publication",
                            "source": relative,
                            "assessment_round": first["round"],
                            "source_round": source_round,
                            "detail": "The original assessment names this later-published file; "
                            "filename identity does not establish byte identity across rounds.",
                        }
                        if limitation not in manifest["limitations"]:
                            manifest["limitations"].append(limitation)
                else:
                    raise ValueError(
                        "Assessment cites ownwork evidence absent from published exports"
                    )
                refs.append(target)
                if target != ref:
                    changes.append(
                        {"field": f"{field}[{index}]", "from": ref, "to": target}
                    )
            normalized[field] = list(dict.fromkeys(refs))
        if manifest["metrics_ref"] not in normalized["evidence"]:
            raise ValueError("Original assessment does not cite the prepared result")
        normalized["input_citations"] = list(dict.fromkeys(input_citations))
        normalized["provenance"] = {
            **provenance,
            "source_ref": str(first["path"]),
            "source_round": first["round"],
            "normalization": changes,
            "input_scope_roots": scope_roots,
        }
        normalized["recovery_limitations"] = manifest["limitations"]
        normalized["boundary_errors"] = (
            [{"origin": "host-publication", "help": original["help"]}]
            if original["help"] != raw["help"]
            else []
        )
        evaluation_path = destination / "evaluation.json"
        write_json(evaluation_path, normalized)
        manifest["evaluation_ref"] = str(evaluation_path)
    except (KeyError, TypeError, ValueError, OSError) as error:
        manifest["warnings"].append(str(error))
    manifest_path = destination / "manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path, manifest
