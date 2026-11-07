"""Bounded, process-local tasks and snapshot-checked Judge edits."""

from collections import Counter
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class TaskRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TaskId
    question: str
    priority: int = 100
    dependencies: tuple[TaskId, ...] = ()


class TaskState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: TaskRequest
    source: Literal["human", "autonomous"]
    revision: int = 1
    status: Literal["pending", "running", "reviewed", "cancelled", "failed"] = "pending"
    record_id: str | None = None


class CreateTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["create"]
    task: TaskRequest


class UpdateTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["update"]
    task: TaskRequest
    expected_revision: int


class CancelTask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["cancel"]
    id: TaskId
    expected_revision: int


PoolChange = CreateTask | UpdateTask | CancelTask


class TaskPool:
    """Keep all IDs for the lifetime of one run; only pending tasks use capacity.

    Dependencies must exist and form a DAG. Cancelled or failed dependencies keep
    their dependents blocked until the Judge explicitly revises those dependents.
    Revisions advance on every mutation, including execution state transitions.
    """

    def __init__(self, human_tasks: Sequence[TaskRequest], max_pending: int = 8):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
        self._tasks: dict[str, TaskState] = {}
        for task in human_tasks:
            if task.id in self._tasks:
                raise ValueError(f"Task ID already exists: {task.id}")
            self._tasks[task.id] = TaskState(request=task, source="human")
        self._validate(self._tasks)

    def snapshot(self) -> list[dict]:
        return [task.model_dump(mode="json") for task in self._tasks.values()]

    def ready(self) -> list[TaskState]:
        return sorted(
            (
                task
                for task in self._tasks.values()
                if task.status == "pending"
                and all(
                    self._tasks[dependency].status == "reviewed"
                    for dependency in task.request.dependencies
                )
            ),
            key=lambda task: (
                task.source != "human",
                task.request.priority,
                task.request.id,
            ),
        )

    def claim(self, task_id: str) -> TaskState:
        if task_id not in {task.request.id for task in self.ready()}:
            raise ValueError(f"Task is not ready: {task_id}")
        return self._replace(task_id, status="running")

    def complete(self, task_id: str, record_id: str) -> TaskState:
        if not record_id:
            raise ValueError("A reviewed task requires a record ID")
        return self._finish(task_id, "reviewed", record_id)

    def fail(self, task_id: str) -> TaskState:
        return self._finish(task_id, "failed", None)

    def add_human(self, task: TaskRequest) -> TaskState:
        if task.id in self._tasks:
            raise ValueError(f"Task ID already exists: {task.id}")
        state = TaskState(request=task, source="human")
        candidate = {**self._tasks, task.id: state}
        self._validate(candidate)
        self._tasks = candidate
        return state

    def apply(self, changes: Sequence[PoolChange], snapshot: list[dict]) -> dict:
        """Apply independent ordered edits, returning conflicts without raising.

        All entries targeting a duplicate ID in this batch are rejected. Update
        and cancel require both the supplied snapshot and current pending task to
        be autonomous and at the expected revision. Creates may depend on earlier
        successful creates in the same batch.
        """
        seen = [TaskState.model_validate(value) for value in snapshot]
        baseline = {state.request.id: state for state in seen}
        if len(baseline) != len(seen):
            raise ValueError("Snapshot contains duplicate IDs")
        targets = [
            change.id if isinstance(change, CancelTask) else change.task.id
            for change in changes
        ]
        duplicates = Counter(targets)
        applied: list[str] = []
        conflicts: list[dict] = []
        for index, (change, task_id) in enumerate(zip(changes, targets, strict=True)):
            try:
                if duplicates[task_id] > 1:
                    raise ValueError("duplicate target in batch")
                if isinstance(change, CreateTask):
                    if task_id in self._tasks:
                        raise ValueError("task ID already exists")
                    replacement = TaskState(request=change.task, source="autonomous")
                else:
                    previous = baseline.get(task_id)
                    current = self._tasks.get(task_id)
                    if previous is None or current is None:
                        raise ValueError("task missing from snapshot or pool")
                    if any(
                        state.source != "autonomous" or state.status != "pending"
                        for state in (previous, current)
                    ):
                        raise ValueError("only pending autonomous tasks may be edited")
                    if not (
                        previous.revision
                        == current.revision
                        == change.expected_revision
                    ):
                        raise ValueError("stale revision")
                    replacement = current.model_copy(
                        update={
                            "revision": current.revision + 1,
                            **(
                                {"status": "cancelled"}
                                if isinstance(change, CancelTask)
                                else {"request": change.task}
                            ),
                        }
                    )
                candidate = {**self._tasks, task_id: replacement}
                self._validate(candidate)
                self._tasks = candidate
                applied.append(task_id)
            except ValueError as exc:
                conflicts.append({"index": index, "id": task_id, "reason": str(exc)})
        return {"applied": applied, "conflicts": conflicts}

    def _replace(self, task_id: str, **updates) -> TaskState:
        current = self._tasks[task_id]
        replacement = current.model_copy(
            update={**updates, "revision": current.revision + 1}
        )
        self._tasks[task_id] = replacement
        return replacement

    def _finish(self, task_id: str, status: str, record_id: str | None) -> TaskState:
        current = self._tasks.get(task_id)
        if current is None or current.status != "running":
            raise ValueError(f"Task is not running: {task_id}")
        return self._replace(task_id, status=status, record_id=record_id)

    def _validate(self, tasks: dict[str, TaskState]) -> None:
        if sum(task.status == "pending" for task in tasks.values()) > self.max_pending:
            raise ValueError("pending capacity exceeded")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id not in tasks:
                raise ValueError(f"missing dependency: {task_id}")
            if task_id in active:
                raise ValueError("dependency cycle")
            if task_id in visited:
                return
            active.add(task_id)
            for dependency in tasks[task_id].request.dependencies:
                visit(dependency)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)
