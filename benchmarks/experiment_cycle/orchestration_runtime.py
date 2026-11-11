"""Fixed phase, execution and receipt authority behind the editable program."""

import asyncio
import json
import re
from time import monotonic

from research_loop.state import write_json

from .runner import STAGES, utc, verify_delivery

MAX_CALLS = 8


class Cycle:
    def __init__(
        self, case, workspace, output, command, agent, executor, model, effort, common
    ):
        self.case, self.workspace, self.output = case, workspace, output
        self.command, self.agent, self.executor = command, agent, executor
        self.model, self.effort, self.common = model, effort, common
        self.roster, self.records, self.calls = [], [], []
        self.current = -1
        self.phase_start = None
        self.deliveries = {}
        self.failure = None
        self.phase_protocol_pass = False

    def publish(self):
        write_json(
            self.output / "tasks.json",
            {"case_id": self.case["case_id"], "tasks": self.roster},
        )
        write_json(self.output / "stages.json", self.records)
        write_json(self.output / "calls.json", self.calls)

    def enter(self, stage):
        index = STAGES.index(stage)
        if index < self.current or index > self.current + 1:
            raise ValueError("Phase order must remain plan, prepare, execute, evaluate")
        if index == self.current:
            return
        if index and STAGES[index - 1] not in self.deliveries:
            raise ValueError("The previous phase has no valid delivery")
        if self.current >= 0:
            self.records[-1].update(
                status="completed",
                ended_at=utc(),
                wall_seconds=monotonic() - self.phase_start,
            )
        self.current = index
        self.phase_start = monotonic()
        self.records.append(
            {
                "stage": stage,
                "status": "running",
                "started_at": utc(),
                "ended_at": None,
                "wall_seconds": 0,
            }
        )
        self.publish()

    async def _invoke(
        self, stage, *, role, context=None, instructions, handoff=None, message=False
    ):
        if stage not in ("plan", "prepare", "evaluate"):
            raise ValueError("Agent calls require a specialist phase")
        for value in (role, context or role):
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", value
            ):
                raise ValueError("Role and context must be simple names")
        if (
            not isinstance(instructions, str)
            or not instructions.strip()
            or len(instructions.encode()) > 16384
        ):
            raise ValueError("Instructions must be a nonempty bounded string")
        if len(json.dumps(handoff).encode()) > 65536:
            raise ValueError("Handoff exceeds the fixed size limit")
        if len(self.roster) >= MAX_CALLS:
            raise ValueError("Fixed model-call budget exhausted")
        self.enter(stage)
        identity = f"call-{len(self.roster) + 1:02d}"
        root = self.output / "calls" / identity
        root.mkdir(parents=True)
        row = {
            "call_id": identity,
            "stage": stage,
            "role": role,
            "context": context or role,
            "thread_id": None,
            "history_path": None,
            "invoked": True,
        }
        self.roster.append(row)
        trace = {
            "call_id": identity,
            "stage": stage,
            "role": role,
            "context": context or role,
            "kind": "message" if message else "delivery",
            "status": "running",
            "started_at": utc(),
        }
        self.calls.append(trace)
        self.publish()

        def associate(tid, path, **window):
            row.update(
                thread_id=tid, history_path=path or row["history_path"], **window
            )
            self.publish()

        started = monotonic()
        try:
            result = await self.agent(
                stage,
                self.case,
                self.workspace,
                root,
                associate,
                self.model,
                self.effort,
                role=role,
                context=context or role,
                common=self.common,
                instructions=instructions,
                handoff=handoff,
                message=message,
            )
            if message:
                if (
                    not isinstance(result, dict)
                    or set(result) != {"message"}
                    or not isinstance(result["message"], str)
                ):
                    raise ValueError("Message calls must return one message string")
                write_json(
                    self.workspace / ".cycle" / f"{identity}-message.json", result
                )
            else:
                verify_delivery(
                    stage,
                    result,
                    self.workspace,
                    self.command,
                    self.case["result_file"],
                )
                self.deliveries[stage] = result
                write_json(self.workspace / ".cycle" / f"{stage}.json", result)
            write_json(root / "delivery.json", result)
            trace["status"] = "completed"
            return json.loads(json.dumps(result))
        except BaseException as error:
            trace.update(status="failed", error=f"{type(error).__name__}: {error}")
            raise
        finally:
            trace.update(ended_at=utc(), wall_seconds=monotonic() - started)
            self.publish()

    async def call(self, stage, *, role, context=None, instructions, handoff=None):
        return await self._invoke(
            stage,
            role=role,
            context=context,
            instructions=instructions,
            handoff=handoff,
        )

    async def message(self, stage, *, role, context=None, instructions, handoff=None):
        return await self._invoke(
            stage,
            role=role,
            context=context,
            instructions=instructions,
            handoff=handoff,
            message=True,
        )

    async def execute(self):
        if "execute" in self.deliveries:
            raise ValueError("The fixed experiment may execute only once")
        self.enter("execute")
        root = self.output / "execute"
        root.mkdir()
        script = self.workspace / self.case["command"][1]
        metadata = script.stat()
        identity = {
            "command": self.command,
            "candidate_path": str(script),
            "candidate_size": metadata.st_size,
            "candidate_mtime_ns": metadata.st_mtime_ns,
            "case_id": self.case["case_id"],
            "source": self.case["source"],
        }
        write_json(root / "dispatch.json", identity)
        result = await self.executor(
            self.case, self.workspace, root, self.command, self.model, self.effort
        )
        result.update(identity)
        write_json(root / "receipt.json", result)
        if result["exit_code"] != 0:
            raise ValueError(f"Experiment exited {result['exit_code']}")
        if not (self.workspace / self.case["result_file"]).is_file():
            raise ValueError("Experiment produced no result")
        self.deliveries["execute"] = result
        write_json(self.workspace / ".cycle/execute.json", result)
        self.publish()
        return json.loads(json.dumps(result))

    async def run(self, program):
        try:
            async with asyncio.timeout(
                MAX_CALLS * self.case["limits"]["stage_seconds"]
                + self.case["limits"]["execute_seconds"]
                + 300
            ):
                result = await program.run(self)
            if set(self.deliveries) != set(STAGES):
                raise ValueError("Orchestration omitted a required phase")
            if result is not None and result != self.deliveries["evaluate"]:
                raise ValueError(
                    "The return value differs from the measured critic delivery"
                )
            self.phase_protocol_pass = True
        except Exception as error:
            self.failure = f"{type(error).__name__}: {error}"
        finally:
            if self.current >= 0:
                self.records[-1].update(
                    status="failed" if self.failure else "completed",
                    ended_at=utc(),
                    wall_seconds=monotonic() - self.phase_start,
                )
                if self.failure:
                    self.records[-1]["error"] = self.failure
            for stage in STAGES[len(self.records) :]:
                self.records.append(
                    {
                        "stage": stage,
                        "status": "not-run",
                        "wall_seconds": 0,
                        "started_at": None,
                        "ended_at": None,
                    }
                )
            self.publish()
