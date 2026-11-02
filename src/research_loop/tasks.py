"""Durable operator requests, bound once to ordinary Study iterations."""

from __future__ import annotations

import fcntl
import json
import math
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _read(path):
    return json.loads(Path(path).read_text())


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _published(root, iteration):
    folder = root / "iterations" / iteration
    if (
        not (folder / "publication.json").exists()
        or not (folder / "iteration.json").exists()
    ):
        return False
    metadata = _read(folder / "iteration.json")
    bundle = _read(folder / "publication.json")
    state = _read(root / "state.json")
    return (
        metadata.get("status") == "reviewed"
        and bundle["metadata"] == metadata
        and (
            int(iteration) < state["iteration"]
            or (int(iteration) == state["iteration"] and state["phase"] == "done")
        )
    )


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
    ):
        raise ValueError(
            "Task id must be 1–64 safe letters, digits, underscores or hyphens"
        )
    return value


class TaskQueue:
    def __init__(self, study_root):
        self.root = Path(study_root).resolve()
        self.lane = self.root / "tasks"

    @contextmanager
    def _lock(self):
        self.lane.mkdir(parents=True, exist_ok=True)
        with (self.lane / "queue.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _folder(self, identity):
        return self.lane / _identifier(identity)

    def _normalize(self, spec):
        allowed = {
            "version",
            "id",
            "kind",
            "title",
            "instruction",
            "route_id",
            "base_experiment",
            "priority",
            "allow_changes",
            "budget",
        }
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValueError("Unknown or read-only task request fields")
        result = {
            "version": 1,
            "kind": "investigation",
            "route_id": "new",
            "base_experiment": None,
            "priority": 100,
            "allow_changes": True,
            "budget": None,
            **spec,
        }
        identity = _identifier(result.get("id"))
        result.setdefault("title", identity)
        if type(result["version"]) is not int or result["version"] != 1:
            raise ValueError("Unsupported task version")
        if result["kind"] not in {"investigation", "long_training"}:
            raise ValueError("Unknown task kind")
        for key in ("title", "instruction", "route_id"):
            if not isinstance(result.get(key), str) or not result[key].strip():
                raise ValueError(f"Task {key} must be nonempty")
        if (
            type(result["priority"]) is not int
            or type(result["allow_changes"]) is not bool
        ):
            raise ValueError("Task priority must be integer and allow_changes boolean")
        base = result["base_experiment"]
        if base is not None and (
            not isinstance(base, str)
            or not re.fullmatch(r"[0-9]{4,}", base)
            or int(base) < 1
        ):
            raise ValueError("Invalid base experiment")
        budget = result["budget"]
        if result["kind"] == "investigation" and budget is not None:
            raise ValueError(
                "Per-task compute budgets are supported only for long_training; "
                "investigations use parent Study grant"
            )
        if result["kind"] == "long_training":
            keys = {
                "authorization",
                "gpu_seconds_per_run",
                "total_gpu_seconds",
                "deadline",
            }
            if not isinstance(budget, dict) or set(budget) != keys:
                raise ValueError(
                    "Long training requires explicit authorization and compute budget"
                )
            per, total, deadline = (
                budget[k]
                for k in ("gpu_seconds_per_run", "total_gpu_seconds", "deadline")
            )
            if (
                not isinstance(budget["authorization"], str)
                or not budget["authorization"].strip()
                or type(per) is not int
                or per <= 0
                or per % 60
                or type(total) is not int
                or total < per
                or type(deadline) not in (int, float)
                or not math.isfinite(deadline)
            ):
                raise ValueError("Invalid task compute budget")
        return result

    def submit(self, spec):
        normalized = self._normalize(spec)
        with self._lock():
            folder = self._folder(normalized["id"])
            path = folder / "request.json"
            if path.exists():
                existing = _read(path)
                if {k: existing[k] for k in normalized} != normalized:
                    raise ValueError("Task id already has a different request")
                return existing
            config = _read(self.root / "study.json")
            source = {
                "project": config["workspace"],
                "workspace": config["workspace"],
                "revision": config.get("baseline"),
                "experiment": normalized["base_experiment"],
            }
            base = normalized["base_experiment"]
            if base:
                if not _published(self.root, base):
                    raise ValueError(
                        "Task base must be a fully published reviewed experiment"
                    )
                metadata = _read(self.root / "iterations" / base / "iteration.json")
                source.update(
                    workspace=metadata["workspace"],
                    revision=metadata.get("workspace_ref"),
                )
            if normalized["route_id"] != "new" and normalized["route_id"] not in _read(
                self.root / "routes/index.json"
            ):
                raise ValueError("Unknown task route")
            if not source["revision"] and base is None:
                project = Path(source["project"])
                if (project / ".git").exists():
                    try:
                        source["revision"] = subprocess.check_output(
                            [
                                "git",
                                "-C",
                                str(project),
                                "rev-parse",
                                "--verify",
                                "HEAD^{commit}",
                            ],
                            text=True,
                            stderr=subprocess.PIPE,
                        ).strip()
                    except subprocess.CalledProcessError as error:
                        raise ValueError(
                            "Task requires a frozen Git source revision"
                        ) from error
            if not source["revision"]:
                raise ValueError("Task requires a frozen Git source revision")
            request = normalized | {
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
            }
            _write(path, request)
            return request

    def request(self, identity):
        return _read(self._folder(identity) / "request.json")

    def snapshot(self):
        rows = []
        for path in sorted(self.lane.glob("*/request.json")):
            request = _read(path)
            folder = path.parent
            binding = (
                _read(folder / "binding.json")
                if (folder / "binding.json").exists()
                else None
            )
            row = {
                "id": request["id"],
                "request": request,
                "binding": binding,
                "priority": _read(folder / "scheduling.json")["priority"]
                if (folder / "scheduling.json").exists()
                else request["priority"],
                "status": "cancelled"
                if (folder / "cancelled.json").exists()
                else "pending",
                "phase": None,
                "result": None,
            }
            if (
                not binding
                and row["status"] == "pending"
                and request["kind"] == "long_training"
                and request["budget"]["deadline"] <= time.time()
            ):
                row["status"] = "expired"
            if binding:
                root, iteration = Path(binding["study"]), binding["iteration"]
                row.update(status="bound", phase="pending")
                state = None
                if (root / "state.json").exists():
                    state = _read(root / "state.json")
                    row.update(
                        status=state["status"],
                        phase=state["phase"] if state["iteration"] else "pending",
                    )
                    if _published(root, iteration):
                        row.update(status="reviewed", phase="done")
                        if request["kind"] == "long_training":
                            bundle = _read(
                                root / "iterations" / iteration / "publication.json"
                            )
                            published = bundle["state_after"]
                            row["result"] = {
                                "id": request["id"],
                                "request": request,
                                "review": _read(root / published["review_ref"]),
                                "execute": _read(root / published["execution_ref"]),
                                "publication_ref": str(
                                    root / "iterations" / iteration / "publication.json"
                                ),
                            }
                pristine = state is None or (
                    state.get("iteration") == 0
                    and state.get("phase") == "done"
                    and state.get("status") in {"ready", "needs_attention"}
                    and state.get("calls") == {}
                    and state.get("call_count") == 0
                    and state.get("active_attempt") is None
                    and state.get("round_admission") is None
                )
                if (
                    request["kind"] == "long_training"
                    and request["budget"]["deadline"] <= time.time()
                    and pristine
                ):
                    row.update(status="expired", phase="done")
            rows.append(row)
        return sorted(
            rows,
            key=lambda row: (
                row["priority"],
                row["request"]["submitted_at"],
                row["id"],
            ),
        )

    def cancel(self, identity):
        with self._lock():
            self.request(identity)
            folder = self._folder(identity)
            if (folder / "binding.json").exists():
                raise ValueError("A bound task cannot be cancelled")
            _write(folder / "cancelled.json", {"id": identity})
        return next(row for row in self.snapshot() if row["id"] == identity)

    def set_priority(self, identity, priority):
        if type(priority) is not int:
            raise ValueError("Task priority must be an integer")
        with self._lock():
            self.request(identity)
            folder = self._folder(identity)
            if (folder / "binding.json").exists() or (
                folder / "cancelled.json"
            ).exists():
                raise ValueError("Only pending tasks may be reprioritized")
            _write(folder / "scheduling.json", {"priority": priority})
        return next(row for row in self.snapshot() if row["id"] == identity)

    def claim(self, kind, *, iteration=None, allow_new=True):
        if kind not in {"investigation", "long_training"}:
            raise ValueError("Unknown task kind")
        if kind == "investigation" and (type(iteration) is not int or iteration < 1):
            raise ValueError("Investigation claim requires a positive iteration")
        with self._lock():
            rows = [row for row in self.snapshot() if row["request"]["kind"] == kind]
            for row in rows:
                binding = row["binding"]
                if binding and (
                    (
                        kind == "investigation"
                        and binding["iteration"] == f"{iteration:04d}"
                    )
                    or (
                        kind == "long_training"
                        and row["status"] != "reviewed"
                        and row["phase"] != "done"
                    )
                ):
                    return row["request"]
            if not allow_new:
                return None
            row = next((row for row in rows if row["status"] == "pending"), None)
            if row is None:
                return None
            folder = self._folder(row["id"])
            binding = {
                "study": str(
                    self.root if kind == "investigation" else folder / "study"
                ),
                "iteration": f"{iteration:04d}" if kind == "investigation" else "0001",
            }
            _write(folder / "binding.json", binding)
            return row["request"]


def completed_results(study_root):
    return [
        row["result"]
        for row in TaskQueue(study_root).snapshot()
        if row["request"]["kind"] == "long_training" and row["status"] == "reviewed"
    ]
