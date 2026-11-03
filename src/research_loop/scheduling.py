"""Dispatch independent child Studies without owning their execution state."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import threading
import time
import uuid


def _state(folder):
    path = Path(folder) / "study/state.json"
    return json.loads(path.read_text()) if path.exists() else None


def recorded_child_error(folder):
    """Expose a retained scheduling failure without labeling a live Study."""
    folder = Path(folder)
    marker = folder / "scheduler-error.json"
    if marker.exists():
        error = json.loads(marker.read_text())
        if error["state"] == _state(folder):
            return error["error"]
    return None


def child_blocked(folder):
    """A failure stays local until an explicit retry changes the retained state."""
    folder = Path(folder)
    try:
        state = _state(folder)
        error = recorded_child_error(folder)
        if error:
            return error
        if state and state["status"] in {"needs_attention", "running", "stopped"}:
            return (
                state.get("error")
                or state.get("stop_reason")
                or ("Retained child requires explicit reconciliation")
            )
    except (OSError, ValueError, KeyError, TypeError) as error:
        return f"{type(error).__name__}: {error}"
    return None


def record_child_error(folder, error):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        state = _state(folder)
    except (OSError, ValueError):
        state = None
    marker = folder / "scheduler-error.json"
    temporary = folder / f".scheduler-error-{uuid.uuid4().hex}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "error": f"{type(error).__name__}: {error}",
                "state": state,
                "at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    temporary.replace(marker)
    # The dispatcher must not snapshot a newer explicit retry after ownership
    # has been released and attach this older failure to it.
    if isinstance(error, BaseException):
        error._child_scheduler_recorded = True


def clear_child_error(folder):
    (Path(folder) / "scheduler-error.json").unlink(missing_ok=True)


@contextmanager
def child_run(folder):
    """Dispatch and explicit recovery share ownership of exactly one child."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "runner.lock").open("a") as lock:
        # Contention is another owner, not a failed experiment.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        except BlockingIOError:
            raise
        except (Exception, KeyboardInterrupt) as error:
            record_child_error(folder, error)
            raise
        else:
            state = _state(folder)
            published = (
                state
                and state.get("phase") == "done"
                and (
                    folder
                    / "study/iterations"
                    / f"{state.get('iteration', 0):04d}"
                    / "publication.json"
                ).exists()
            )
            if (
                state
                and state.get("status") in {"needs_attention", "stopped"}
                and not published
            ):
                record_child_error(
                    folder, RuntimeError("Child stopped before reviewed publication")
                )
            else:
                clear_child_error(folder)


class AsyncStudyScheduler:
    """Poll a finite child queue; each child owns a separate bounded worker."""

    def __init__(self, root, *, pending, run_one, name, interval=3):
        self.root = Path(root)
        self.pending, self.run_one = pending, run_one
        self.interval = interval
        self.stopped, self.draining = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._workers = {}
        self._status = {"status": "idle", "reason": None}
        self.thread = threading.Thread(target=self._run, daemon=True, name=name)

    def status(self):
        with self._lock:
            return dict(self._status)

    def start(self):
        self.thread.start()

    def finish_nominations(self):
        self.draining.set()

    def _run_child(self, row):
        try:
            result = self.run_one(
                row["id"],
                allow_new=not (self.root / "control/HANDOFF").exists(),
            )
            if result and result.get("status") in {"needs_attention", "stopped"}:
                state = _state(row["folder"])
                if state is None or state.get("status") in {
                    "needs_attention",
                    "stopped",
                }:
                    record_child_error(
                        row["folder"],
                        RuntimeError("Child stopped before reviewed publication"),
                    )
        except BlockingIOError:
            pass
        except (Exception, KeyboardInterrupt) as error:
            if not getattr(error, "_child_scheduler_recorded", False):
                record_child_error(row["folder"], error)

    def _publish(self, status, reason=None, *, active=(), blocked=()):
        with self._lock:
            self._status = {"status": status, "reason": reason}
            if active:
                self._status["active"] = list(active)
            if blocked:
                self._status["blocked"] = list(blocked)

    def _run(self):
        try:
            while not self.stopped.is_set():
                self._workers = {
                    identity: worker
                    for identity, worker in self._workers.items()
                    if worker.is_alive()
                }
                if (self.root / "control/STOP").exists():
                    self._publish("finished", "user_stop")
                    return
                handoff = (self.root / "control/HANDOFF").exists()
                try:
                    rows = self.pending(allow_new=not handoff)
                except Exception as error:
                    self._publish(
                        "running" if self._workers else "blocked",
                        f"{type(error).__name__}: {error}",
                        active=self._workers,
                    )
                    if self.draining.is_set() and not self._workers:
                        return
                    self.stopped.wait(self.interval)
                    continue
                blocked = []
                for row in rows:
                    if row["id"] in self._workers:
                        continue
                    error = child_blocked(row["folder"])
                    if error:
                        blocked.append({"id": row["id"], "reason": error})
                        continue
                    if self.stopped.is_set() or (self.root / "control/STOP").exists():
                        break
                    worker = threading.Thread(
                        target=self._run_child,
                        args=(row,),
                        daemon=True,
                        name=f"{self.thread.name}-{row['id']}",
                    )
                    self._workers[row["id"]] = worker
                    worker.start()
                active = list(self._workers)
                if active:
                    self._publish("running", active=active, blocked=blocked)
                elif blocked:
                    self._publish("blocked", blocked[0]["reason"], blocked=blocked)
                elif self.draining.is_set() or handoff:
                    self._publish("finished", "handoff" if handoff else "no_pending")
                    return
                else:
                    self._publish("idle")
                if not active and blocked and (self.draining.is_set() or handoff):
                    return
                self.stopped.wait(self.interval)
        finally:
            # HANDOFF drains admitted children. STOP cancellation belongs to each
            # child's existing executor, never to a sibling's error handler.
            for worker in self._workers.values():
                worker.join()

    def shutdown(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()
