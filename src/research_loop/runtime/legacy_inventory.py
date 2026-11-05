"""Read-only legacy v3 control inventory, never a recovery or authority fence.

Only named controller documents are read. Workspace paths, artifact references,
logs, imported adapters and child Studies are not followed. File metadata detects
ordinary concurrent changes; it is not a content hash or sender-retirement proof.
"""

import json
import math
from pathlib import Path, PurePosixPath
import re
import stat


_ROOT_FILES = (
    "study.json",
    "state.json",
    "goal.md",
    "observations.json",
    "research.md",
    "frontiers.json",
    "research_inputs.json",
    "execution-backend.json",
    "STOP",
    "control/STOP",
    "control/HANDOFF",
)
_ITERATION_FILES = (
    "iteration.json",
    "publication.json",
    "selection.json",
    "candidates.json",
    "review.before.json",
    "local-evaluations.after.json",
    "insight.json",
    "long-training.json",
    "plan.md",
    "execute.md",
    "review.md",
    "global.after.md",
    "route.after.md",
)
_CONTEXT_FILES = (
    "goal.md",
    "study.before.json",
    "global.before.md",
    "routes.before.json",
    "frontiers.before.json",
    "task.json",
    "task_results.json",
    "route.before.md",
    "long_training_results.json",
    "research_inputs.json",
    "scout_inputs.json",
    "recent_insights.json",
    "telemetry.json",
    "manual-long-request.json",
)
_ATTEMPT_FILES = (
    "context.json",
    "context-manifest.json",
    "schema.json",
    "process.json",
    "response.json",
    "completed.json",
    "canonical-response.json",
    "parameter-budget.json",
    "feedback.json",
    "rejection.json",
    "failure.json",
    "returned.json",
)
_TASK_FILES = ("request.json", "binding.json", "scheduling.json", "cancelled.json")
_IGNORED_NAMES = {".lock", "queue.lock"}
_ROLES = {"plan", "execute", "review"}
_ATTEMPT_PATTERN = (
    r"iterations/[0-9]{4,}/(?:"
    r"(?:plan|execute|review)/attempt-[0-9]+|execute/recovery|"
    r"selection/round-[0-9]+/(?:[ab]-proposal|[0-9]+-[ab]-(?:critique|response)|judge)/attempt-[0-9]+)"
)


def _attempt_reference(reference):
    return re.fullmatch(_ATTEMPT_PATTERN, reference) is not None


def _directory_identity(metadata):
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _file_identity(metadata):
    return _directory_identity(metadata) | {
        "mode": metadata.st_mode,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }


def _path(root, relative):
    value = PurePosixPath(relative)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ValueError(f"Control reference must stay inside the Study: {relative}")
    current = root
    for part in value.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlink in legacy control path: {relative}")
    return current


def _metadata(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _annex_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("Annex source must be an absolute controller path")
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Annex source must not contain parent traversal")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Annex source must not traverse symlinks")
    return path


def _annex_records(inventory):
    annex = inventory.get("annex")
    if annex is None:
        return {}
    if (
        not isinstance(annex, dict)
        or set(annex) != {"version", "records"}
        or type(annex["version"]) is not int
        or annex["version"] != 1
        or not isinstance(annex["records"], dict)
    ):
        raise ValueError("Unsupported captured control annex")
    for name, record in annex["records"].items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
        ):
            raise ValueError("Annex logical names must be single safe segments")
        if not isinstance(record, dict) or set(record) != {
            "source_path",
            "identity",
            "text",
        }:
            raise ValueError("Invalid captured annex record")
    return annex["records"]


def capture_control_annex(inventory, paths):
    """Attach explicitly selected small controller documents outside the Study.

    The caller must select control JSON/text, never datasets or computational
    artifacts. No directory discovery, hooks, hashes or authority inference run.
    Already transferred captures can use this same annex schema without reread.
    """
    result = json.loads(json.dumps(inventory, allow_nan=False))
    records = _annex_records(result)
    if not isinstance(paths, dict):
        raise ValueError("Annex paths must map logical names to absolute paths")
    for name, value in paths.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", name
        ):
            raise ValueError("Annex logical names must be single safe segments")
        path = _annex_path(value)
        before = _metadata(path)
        if before is None:
            record = {"source_path": str(path), "identity": None, "text": None}
        else:
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Annex control document must be a regular file")
            text = path.read_bytes().decode("utf-8")
            after = _metadata(_annex_path(str(path)))
            if after is None or _file_identity(before) != _file_identity(after):
                raise ValueError("Annex control document changed during capture")
            record = {
                "source_path": str(path),
                "identity": _file_identity(before),
                "text": text,
            }
        if name in records and records[name] != record:
            raise ValueError("Annex logical name already has a different binding")
        records[name] = record
    result["annex"] = {"version": 1, "records": records}
    return result


def _directory(root, relative):
    path = _path(root, relative)
    before = _metadata(path)
    if before is None:
        return {"identity": None, "entries": None}
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"Expected legacy control directory: {relative}")
    entries = sorted(
        item.name for item in path.iterdir() if item.name not in _IGNORED_NAMES
    )
    after = _metadata(_path(root, relative))
    if after is None or _file_identity(before) != _file_identity(after):
        raise ValueError(f"Legacy directory changed while reading: {relative}")
    return {"identity": _directory_identity(before), "entries": entries}


def verify_inventory(root, inventory, *, allow_runtime_handoff=False):
    """Check source and captured metadata only; never reread a document body.

    Lock acquisition and cutover belong to the caller. The root entry list and
    lockfiles are deliberately not fenced, allowing staging and lock creation.
    The caller may skip the two runtime markers only after authenticating its
    matching handoff marker under the source locks; this function reads no body.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Legacy Study root must not be a symlink")
    root = root.resolve()
    metadata = root.stat()
    if (
        inventory.get("version") != 1
        or inventory.get("source_root") != str(root)
        or inventory.get("source_identity") != _directory_identity(metadata)
    ):
        raise ValueError("Legacy source identity differs from inventory")
    for relative, expected in inventory["records"].items():
        if allow_runtime_handoff and relative in {"runtime.json", "runtime.sqlite3"}:
            continue
        actual = _metadata(_path(root, relative))
        identity = _file_identity(actual) if actual is not None else None
        if identity != expected["identity"]:
            raise ValueError(f"Legacy control file changed: {relative}")
    for relative, expected in inventory["directories"].items():
        actual = _directory(root, relative)
        if (
            relative == "tasks"
            and expected == {"identity": None, "entries": None}
            and actual["identity"] is not None
            and actual["entries"] == []
        ):
            # Legacy TaskQueue has its own lock. Acquiring it may create just
            # this formerly absent directory and its regular queue.lock file.
            path = _path(root, relative)
            entries = list(path.iterdir())
            if all(
                item.name == "queue.lock" and stat.S_ISREG(item.lstat().st_mode)
                for item in entries
            ):
                continue
        if actual != expected:
            raise ValueError(f"Legacy control directory changed: {relative}")
    for name, expected in _annex_records(inventory).items():
        actual = _metadata(_annex_path(expected["source_path"]))
        identity = _file_identity(actual) if actual is not None else None
        if identity != expected["identity"]:
            raise ValueError(f"Captured annex control changed: annex:{name}")


class _Capture:
    def __init__(self, root):
        self.root = root
        self.records = {}
        self.directories = {}
        self.decoded = {}
        self.blockers = []
        self.attempts = set()

    def block(self, reason):
        if reason not in self.blockers:
            self.blockers.append(reason)

    def file(self, relative):
        if relative in self.records:
            return self.decoded.get(relative)
        path = _path(self.root, relative)
        before = _metadata(path)
        if before is None:
            self.records[relative] = {"identity": None, "text": None}
            return None
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Expected regular legacy control file: {relative}")
        # Bytes->UTF-8 keeps newline sequences exactly, unlike universal-newline
        # read_text. This reads the requested control input, never artifact data.
        text = path.read_bytes().decode("utf-8")
        after = _metadata(_path(self.root, relative))
        if after is None or _file_identity(before) != _file_identity(after):
            raise ValueError(f"Legacy control file changed while reading: {relative}")
        self.records[relative] = {"identity": _file_identity(before), "text": text}
        if relative.endswith(".json"):
            try:
                value = json.loads(text, parse_constant=_invalid_constant)
            except ValueError:
                self.block(f"invalid_json:{relative}")
                value = None
        else:
            value = text
        self.decoded[relative] = value
        return value

    def directory(self, relative):
        if relative not in self.directories:
            self.directories[relative] = _directory(self.root, relative)
        return self.directories[relative]["entries"] or []

    def known(self, relative, names, ignored=()):
        entries = self.directory(relative)
        for name in names:
            self.file(f"{relative}/{name}")
        for name in entries:
            if name not in names and name not in ignored:
                self.block(f"unsupported_control_entry:{relative}/{name}")
        return entries

    def attempt(self, relative):
        if relative in self.attempts:
            return
        self.attempts.add(relative)
        entries = self.directory(relative)
        for name in _ATTEMPT_FILES:
            self.file(f"{relative}/{name}")
        # Adapter delegates sometimes keep their local process under agent/.
        # Capture only the same named receipts, without loading adapter code.
        if "agent" in self.directory(relative):
            self.directory(f"{relative}/agent")
            for name in _ATTEMPT_FILES:
                self.file(f"{relative}/agent/{name}")
        ignored = {"agent", "prompt.md", "stdout.jsonl", "stderr.txt"}
        for name in entries:
            if name not in _ATTEMPT_FILES and name not in ignored:
                self.block(f"unsupported_control_entry:{relative}/{name}")

    def phase(self, relative):
        for name in self.directory(relative):
            if re.fullmatch(r"attempt-[0-9]+", name):
                self.attempt(f"{relative}/{name}")
            elif name == "recovery" and relative.endswith("/execute"):
                self.attempt(f"{relative}/recovery")
                self.block(f"execution_recovery_requires_adapter:{relative}/recovery")
            else:
                self.block(f"unsupported_control_entry:{relative}/{name}")

    def reference(self, reference):
        if reference is None:
            return
        if not isinstance(reference, str):
            self.block("invalid_control_reference")
            return
        _path(self.root, reference)
        parts = PurePosixPath(reference).parts
        allowed = (
            (
                len(parts) == 2
                and parts[0] in {"recoveries", "diagnostics"}
                and parts[-1].endswith(".json")
            )
            or (
                len(parts) == 3
                and parts[0] == "iterations"
                and re.fullmatch(r"[0-9]{4,}", parts[1])
                and parts[-1] in _ITERATION_FILES
            )
            or (
                len(parts) == 4
                and parts[0] == "iterations"
                and re.fullmatch(r"[0-9]{4,}", parts[1])
                and parts[2] == "context"
                and parts[-1] in _CONTEXT_FILES
            )
            or (
                parts[-1] in _ATTEMPT_FILES
                and _attempt_reference(str(PurePosixPath(reference).parent))
            )
            or (
                len(parts) == 2
                and parts[0] == "revisions"
                and parts[-1].endswith((".md", ".md.json"))
            )
        )
        if allowed:
            self.file(reference)
            if self.records[reference]["identity"] is None:
                self.block(f"missing_control_reference:{reference}")
        else:
            self.block(f"unsupported_control_reference:{reference}")

    def state(self, state):
        if not isinstance(state, dict):
            return
        calls = state.get("calls", {})
        if isinstance(calls, dict):
            for reference in calls.values():
                if isinstance(reference, str):
                    _path(self.root, reference)
                    if _attempt_reference(reference):
                        self.attempt(reference)
                    else:
                        self.block(f"unsupported_attempt_reference:{reference}")
                else:
                    self.block("invalid_attempt_reference")
        else:
            self.block("invalid_calls_mapping")
        active = state.get("active_attempt")
        if active is not None:
            if isinstance(active, str):
                _path(self.root, active)
            if isinstance(active, str) and _attempt_reference(active):
                self.attempt(active)
            else:
                self.block("unsupported_active_attempt_reference")
        for key in (
            "plan_ref",
            "execution_ref",
            "review_ref",
            "debugging",
            "execution_recovery_ref",
            "diagnostic_ref",
        ):
            self.reference(state.get(key))


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON number: {value}")


def _publication(capture, relative, metadata, state):
    reference = f"{relative}/publication.json"
    bundle = capture.decoded.get(reference)
    if bundle is None:
        if metadata.get("status") == "reviewed":
            capture.block(f"missing_publication:{relative}")
        return "absent"
    if not isinstance(bundle, dict) or not all(
        isinstance(bundle.get(key), dict)
        for key in ("before", "writes", "metadata", "state_after")
    ):
        capture.block(f"invalid_publication:{relative}")
        return "invalid"
    capture.state(bundle["state_after"])
    number = int(relative.split("/")[-1])
    current = state.get("iteration")
    if (
        type(current) is int
        and metadata.get("status") == "reviewed"
        and metadata == bundle["metadata"]
        and (number < current or (number == current and state.get("phase") == "done"))
    ):
        return "settled"
    capture.block(f"partial_publication:{relative}")
    if number == current:
        for path, old in bundle["before"].items():
            if path not in capture.records:
                capture.block(f"unsupported_publication_target:{path}")
                continue
            value = capture.records[path]["text"]
            if value not in (old, bundle["writes"].get(path, old)):
                capture.block(f"publication_conflict:{path}")
                return "conflicted"
    return "prepared"


def _attempt_history(capture, state):
    rows = []
    accepted = state.get("accepted_calls", {})
    calls = state.get("calls", {})
    pending = (
        {
            reference
            for key, reference in calls.items()
            if isinstance(reference, str) and accepted.get(key) is not True
        }
        if isinstance(calls, dict) and isinstance(accepted, dict)
        else set()
    )
    for reference in sorted(capture.attempts):
        process = capture.decoded.get(f"{reference}/process.json")
        process = process if isinstance(process, dict) else {}
        receipt = capture.decoded.get(f"{reference}/completed.json")
        raw = capture.decoded.get(f"{reference}/response.json")
        completed = (
            isinstance(receipt, dict)
            and receipt.get("version") == 1
            and receipt.get("run_id") == process.get("run_id")
            and isinstance(receipt.get("response"), dict)
        ) or (
            receipt is None
            and not process.get("run_id")
            and process.get("status") == "succeeded"
            and isinstance(raw, dict)
        )
        rejected = (
            capture.records[f"{reference}/rejection.json"]["identity"] is not None
        )
        if not completed or process.get("status") == "running":
            capture.block(f"unknown_attempt_retirement:{reference}")
        if rejected and reference in pending:
            capture.block(f"rejected_attempt_requires_reconciliation:{reference}")
        for candidate in (reference, f"{reference}/agent"):
            child = capture.decoded.get(f"{candidate}/process.json")
            if isinstance(child, dict) and child.get("status") == "running":
                capture.block(f"unknown_attempt_retirement:{candidate}")
        rows.append(
            {
                "ref": reference,
                "completed": completed,
                "process_status": process.get("status"),
                "rejected": rejected,
            }
        )
    return rows


def capture_legacy(root):
    """Capture explicit v3 control records and describe conversion blockers.

    The result is JSON data, preserving exact UTF-8 text alongside summaries.
    No locks, backend hooks, process checks, invocation or publication replay run.
    A caller must establish authority and reverify before an actual cutover.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Legacy Study root must not be a symlink")
    root = root.resolve()
    source = root.stat()
    if not stat.S_ISDIR(source.st_mode):
        raise ValueError("Legacy Study root must be a directory")
    capture = _Capture(root)
    for marker in ("runtime.json", "runtime.sqlite3"):
        if _metadata(_path(root, marker)) is not None:
            raise ValueError("Source already contains SQLite runtime control files")
        capture.records[marker] = {"identity": None, "text": None}
    for path in _ROOT_FILES:
        capture.file(path)
    config, state = capture.decoded.get("study.json"), capture.decoded.get("state.json")
    if (
        not isinstance(config, dict)
        or type(config.get("version")) is not int
        or config["version"] != 3
    ):
        raise ValueError("Legacy inventory requires Study version 3")
    if not isinstance(state, dict):
        raise ValueError("Legacy inventory requires an object state.json")
    capture.file("routes/index.json")
    for path in ("goal.md", "research.md", "frontiers.json", "routes/index.json"):
        if capture.records[path]["identity"] is None:
            capture.block(f"missing_control_file:{path}")
    for name in capture.directory("routes"):
        if name == "index.json":
            continue
        capture.known(f"routes/{name}", ("research.md", "evaluation.json"))
    routes = capture.decoded.get("routes/index.json")
    if isinstance(routes, dict):
        for route in routes.values():
            if isinstance(route, dict):
                for key in ("research_ref", "evaluation_ref"):
                    capture.reference(route.get(key))
    for name in capture.directory("revisions"):
        if name.endswith((".md", ".md.json")):
            capture.file(f"revisions/{name}")
        else:
            capture.block(f"unsupported_control_entry:revisions/{name}")
    tasks = []
    for name in capture.directory("tasks"):
        relative = f"tasks/{name}"
        capture.known(relative, _TASK_FILES)
        request = capture.decoded.get(f"{relative}/request.json")
        binding = capture.decoded.get(f"{relative}/binding.json")
        cancelled = (
            capture.records[f"{relative}/cancelled.json"]["identity"] is not None
        )
        tasks.append(
            {
                "id": name,
                "request_ref": f"{relative}/request.json",
                "status": "bound"
                if binding
                else "cancelled"
                if cancelled
                else "pending",
                "binding": binding,
                "kind": request.get("kind") if isinstance(request, dict) else None,
            }
        )
        if binding or (
            isinstance(request, dict) and request.get("kind") == "long_training"
        ):
            capture.block(f"legacy_task_requires_adapter:{relative}")
    iterations, publications = [], []
    for name in capture.directory("iterations"):
        if not re.fullmatch(r"[0-9]{4,}", name):
            capture.block(f"unsupported_iteration_entry:{name}")
            continue
        relative = f"iterations/{name}"
        capture.known(
            relative,
            _ITERATION_FILES,
            ("context", "selection", "plan", "execute", "review", "workspace"),
        )
        capture.known(f"{relative}/context", _CONTEXT_FILES)
        for role in sorted(_ROLES):
            capture.phase(f"{relative}/{role}")
        for round_name in capture.directory(f"{relative}/selection"):
            round_ref = f"{relative}/selection/{round_name}"
            if not re.fullmatch(r"round-[0-9]+", round_name):
                capture.block(f"unsupported_control_entry:{round_ref}")
                continue
            for phase in capture.directory(round_ref):
                if re.fullmatch(
                    r"(?:[ab]-proposal|[0-9]+-[ab]-(?:critique|response)|judge)", phase
                ):
                    capture.phase(f"{round_ref}/{phase}")
                else:
                    capture.block(f"unsupported_control_entry:{round_ref}/{phase}")
        metadata = capture.decoded.get(f"{relative}/iteration.json")
        if not isinstance(metadata, dict):
            capture.block(f"missing_iteration_metadata:{relative}")
            metadata = {}
        status = _publication(capture, relative, metadata, state)
        iterations.append(
            {
                "id": name,
                "status": metadata.get("status"),
                "publication": status,
                "metadata_ref": f"{relative}/iteration.json",
            }
        )
        if status != "absent":
            publications.append(
                {
                    "iteration_id": name,
                    "ref": f"{relative}/publication.json",
                    "status": status,
                }
            )
        continuation = metadata.get("continuation")
        if isinstance(continuation, dict):
            capture.reference(continuation.get("plan_ref"))
            capture.block(f"execution_continuation_requires_adapter:{relative}")
    capture.state(state)
    for name in capture.directory("recoveries"):
        if name.endswith(".json"):
            reference = f"recoveries/{name}"
            recovery = capture.file(reference)
            if isinstance(recovery, dict):
                capture.state(recovery.get("state_before"))
                capture.state(recovery.get("state_after"))
            capture.block(f"execution_recovery_requires_adapter:{reference}")
        else:
            capture.block(f"unsupported_control_entry:recoveries/{name}")
    capture.known("control", ("STOP", "HANDOFF"))
    if capture.directory("long-runs"):
        capture.block("unsupported_child_studies:long-runs")
    observations = capture.decoded.get("observations.json")
    if isinstance(observations, dict):
        for field in ("runs", "intervals"):
            for row in observations.get(field, []):
                if isinstance(row, dict) and row.get("wall_seconds") is None:
                    capture.block(f"unknown_elapsed_interval:{row.get('id', field)}")
    attempts = _attempt_history(capture, state)
    if (
        state.get("status") in {"running", "needs_attention"}
        or state.get("active_attempt")
        or state.get("debugging")
    ):
        capture.block("unresolved_native_state")
    for key in ("iteration", "call_count", "elapsed_seconds"):
        value = state.get(key)
        if (
            type(value) not in ((int, float) if key == "elapsed_seconds" else (int,))
            or not math.isfinite(value)
            or value < 0
        ):
            capture.block(f"invalid_counter:{key}")
    for key in ("max_iterations", "max_calls", "max_seconds"):
        value = config.get(key)
        if value is None and key != "max_seconds":
            continue
        if type(value) is not int or value < 1:
            capture.block(f"invalid_limit:{key}")
    if state.get("phase") not in {
        "done",
        "selection",
        "plan",
        "execute",
        "review",
    } or state.get("status") not in {"ready", "running", "needs_attention", "stopped"}:
        capture.block("unsupported_native_state")
    if (
        type(state.get("iteration")) is int
        and state["iteration"] > 0
        and not any(row["id"] == f"{state['iteration']:04d}" for row in iterations)
    ):
        capture.block("missing_current_iteration_metadata")
    if (
        iterations
        or tasks
        or attempts
        or state.get("iteration")
        or state.get("call_count")
    ):
        capture.block("scientific_translation_requires_explicit_adapter")
    result = {
        "version": 1,
        "source_root": str(root),
        "source_identity": _directory_identity(source),
        "authority_fenced": False,
        "records": capture.records,
        "directories": capture.directories,
        "counters": {
            "iterations": state.get("iteration"),
            "calls": state.get("call_count"),
            "elapsed_seconds": state.get("elapsed_seconds"),
        },
        "limits": {
            key: config.get(key)
            for key in ("max_iterations", "max_calls", "max_seconds")
        }
        | {"admission_deadline": None},
        "native_state": state,
        "blockers": capture.blockers,
        "history": {
            "iterations": iterations,
            "tasks": tasks,
            "attempt_refs": sorted(capture.attempts),
            "attempts": attempts,
            "publications": publications,
        },
    }
    verify_inventory(root, result)
    return result
