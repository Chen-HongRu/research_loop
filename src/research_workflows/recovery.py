"""Recover settled experiment commands for independent evaluation without redispatch."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from research_loop.state import write_json

_LIMIT = 4 * 1024 * 1024


def _small(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
        raise ValueError("Recovery requires a small regular evidence file")
    with path.open("rb") as stream:
        content = stream.read(_LIMIT + 1)
    if len(content) > _LIMIT:
        raise ValueError("Recovery evidence exceeded the small-file limit")
    return content


def recover_experiment(root, record):
    """Freeze only bounded settled command evidence, without executing or evaluating.

    The execution ledger is authoritative. Missing/unknown operations never become
    dispatchable recovery inputs. This one-time stop path reads selected small
    Plan/Prepare/result files; it never scans a dataset or checkpoint directory.
    """
    from research_workflows.programs.exp import _reference

    if record.get("group_id") != "exp" or record.get("status") == "running":
        return None
    root = Path(root)
    run_root = root / "group-runs" / record["run_id"]
    state = json.loads((run_root / "state.json").read_text())
    phases = state.get("experiment_phases", {})
    if not {"plan", "prepare", "execution"} <= phases.keys():
        return None
    operations = json.loads((run_root / "executions.json").read_text())
    operation_id = "prepared-experiment"
    operation = operations.get(operation_id)
    if not operation or operation.get("status") != "completed":
        return None
    receipt = operation.get("receipt")
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("exit_code")) is not int
        or receipt["exit_code"] != 0
        or receipt.get("running")
        or receipt.get("unknown")
        or receipt.get("error")
        or {
            key: value for key, value in phases["execution"].items() if key != "backend"
        }
        != receipt
    ):
        return None
    destination = root / "recovery" / record["run_id"]
    manifest_path = destination / "recovery.json"
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text())
    work = run_root / "work"
    refs = [phases["plan"]["plan_artifact"], phases["prepare"]["result_file"]]
    refs.extend(phases["plan"].get("artifacts", []))
    refs.extend(phases["prepare"].get("artifacts", []))
    files, total = [], 0
    for ref in dict.fromkeys(refs):
        relative, _ = _reference(SimpleNamespace(work=work), ref)
        source = work / relative
        content = _small(source)
        total += len(content)
        if total > 16 * 1024 * 1024:
            raise ValueError(
                "Settled recovery evidence exceeds the 16 MiB small-file budget"
            )
        target = destination / "files" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        files.append({"source": str(target), "destination": relative})
    operation = deepcopy(operation)
    # The immediate evidence predecessor may itself have adopted this command.
    # Preserve its authoritative dispatch identity and original revision.
    operation.setdefault("source_config_revision", record["config_revision"])
    recovered = {
        "kind": "settled-exp-v1",
        "source_run_id": record["run_id"],
        "source_config_revision": record["config_revision"],
        "source_task_ref": record["task_ref"],
        "source_task_text": _small(Path(record["task_ref"])).decode("utf-8"),
        "source_group_settings": json.loads((run_root / "definition.json").read_text())[
            "settings"
        ],
        "operation_id": operation_id,
        "operation": operation,
        "files": files,
        "phases": {
            name: deepcopy(phases[name]) for name in ("plan", "prepare", "execution")
        },
        "validation": deepcopy(state.get("experiment_phase_validation", {})),
        "history": {
            name: deepcopy(state.get("experiment_phase_history", {}).get(name, []))
            for name in ("plan", "prepare")
        },
        "study_context": deepcopy(state.get("research_context", {})),
        "research_steps": deepcopy(state.get("research_steps", {})),
        "descriptor_ref": str(manifest_path),
        "limitations": [
            "Settled command evidence only; independently unevaluated and unpublished."
        ],
    }
    destination.mkdir(parents=True, exist_ok=True)
    write_json(manifest_path, recovered)
    return recovered


def adopt_experiment(ctx, packet, payload):
    """Seed completed phases in a new Run and preserve the original operation ID."""

    if not isinstance(packet, dict) or packet.get("kind") != "settled-exp-v1":
        raise ValueError("Exp recovery requires an explicit settled-exp-v1 descriptor")
    if ctx.state.get("recovery_source"):
        if ctx.state["recovery_source"] != packet["source_run_id"]:
            raise ValueError("Recovery cannot change its source experiment")
        return
    if ctx.state.get("experiment_phases"):
        raise ValueError("Recovery may seed only a new experiment Run")
    if (
        payload["task_ref"] != packet["source_task_ref"]
        or _small(Path(payload["task_ref"])).decode("utf-8")
        != packet["source_task_text"]
    ):
        raise ValueError(
            "Experiment recovery cannot change the original scientific task"
        )
    if ctx.definition.settings != packet["source_group_settings"]:
        raise ValueError(
            "Experiment recovery cannot change scientific protocol settings"
        )
    if payload.get("study_context", {}).get("output_schema") != packet[
        "study_context"
    ].get("output_schema"):
        raise ValueError(
            "Experiment recovery cannot change the scientific result contract"
        )
    bundle = Path(packet["descriptor_ref"]).parent.resolve(strict=True)
    # Consume the frozen descriptor rather than a caller's mutable object fields.
    frozen = json.loads((bundle / "recovery.json").read_text())
    if frozen != packet:
        raise ValueError("Recovery descriptor differs from its frozen evidence")
    for entry in packet["files"]:
        relative = Path(entry["destination"])
        source = Path(entry["source"])
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("Recovered files must remain inside the new Run work")
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(
            bundle
        ):
            raise ValueError("Recovered evidence must belong to the frozen bundle")
        target = ctx.work / relative
        if target.exists():
            raise ValueError("Recovery must not overwrite work in the new Run")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_small(source))
    ctx.services.adopt_execution(packet["operation_id"], packet["operation"])
    ctx.state["experiment_phases"] = deepcopy(packet["phases"])
    ctx.state["experiment_phase_history"] = deepcopy(packet["history"])
    ctx.state["experiment_phase_validation"] = deepcopy(packet["validation"])
    ctx.state["research_context"] = deepcopy(packet["study_context"])
    ctx.state["research_steps"] = deepcopy(packet["research_steps"])
    ctx.state["recovery_source"] = packet["source_run_id"]
    ctx.save_state()
