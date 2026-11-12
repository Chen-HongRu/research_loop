"""One controller owns admission, interventions and publication; Groups own work."""

import argparse
import asyncio
import json
import os
import signal
import socket
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from . import application, environment, publication
from .codex import Native
from .group_agents import NativeGroupServices
from .group_router import Router, packet
from .groups import GroupDefinition, GroupRuntime
from .interventions import get_intervention, pending_interventions, record_intervention
from .protocol import load_workflow
from .state import (
    Journal,
    PublicationStore,
    PublicationStopped,
    now,
    record_status,
    write_json,
    writer,
)


class Controller:
    def __init__(self, root, *, native_factory=Native):
        self.root = Path(root).resolve(strict=True)
        self.native_factory = native_factory
        self.configs, self.workflows, self.adapters = {}, {}, {}
        self.adapter_owners, self.adapter_closing = {}, {}
        self.config = self._config()
        self.workflow = self._workflow(self.config["revision"])
        self.resource_lock = asyncio.Lock()
        self.state = json.loads((self.root / "status.json").read_text())
        path = self.root / "routing.json"
        self.routing = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "iteration": 1,
                "admitted": [],
                "tasks": [],
                "active_input": None,
                "recoveries": [],
            }
        )
        self.runtime = GroupRuntime(
            self.root,
            self._definitions(),
            self._services,
            revision=self.config["revision"],
        )
        self.wake = asyncio.Event()
        self.stopping = False
        self.context = {}
        self.channel = None
        self.address = None
        self.generation = 0
        self.committing = False
        self.journal = Journal(self.root / "events.jsonl")
        self.router = Router(self.root, self.journal, native_factory=native_factory)

    def _config(self, revision=None):
        if revision is None:
            revision = json.loads((self.root / "active.json").read_text())["revision"]
        if revision not in self.configs:
            config = application.configuration(self.root, revision)
            config["execution_environment"] = environment.observe(
                config.get("environment"), resource_config=config.get("resource")
            )
            self.configs[revision] = config
        return self.configs[revision]

    def _workflow(self, revision):
        if revision not in self.workflows:
            self.workflows[revision] = load_workflow(self._config(revision))
        return self.workflows[revision]

    def _definitions(self):
        return [GroupDefinition.from_json(row) for row in self.config["groups"]]

    def _adapter(self, config):
        if not config.get("resource"):
            return None
        key = json.dumps(config["resource"], sort_keys=True)
        if key not in self.adapters:
            from .slurm import Slurm

            self.adapters[key] = Slurm(config["resource"], self.root)
            self.adapter_closing.pop(key, None)
        return self.adapters[key]

    def _close_adapter(self, key):
        prior = self.adapter_closing.get(key)
        if prior is not None:
            return prior
        adapter = self.adapters[key]

        async def close():
            result = await adapter.close()
            settled = not result.get("cleanup_errors") and all(
                row["status"] == "TERMINAL" for row in result["reservations"]
            )
            if settled and self.adapters.get(key) is adapter:
                self.adapters.pop(key)
                self.adapter_owners.pop(key, None)
            return result

        task = asyncio.create_task(close())
        self.adapter_closing[key] = task
        return task

    def _services(self, ctx):
        config = self._config(ctx.config_revision)
        adapter = self._adapter(config)
        tools = []
        if adapter is not None:
            key = json.dumps(config["resource"], sort_keys=True)
            for tool in adapter.tools():

                async def call(arguments, name=tool.name):
                    closing = self.adapter_closing.get(key)
                    if closing is not None:
                        await asyncio.shield(closing)
                    current = self._adapter(config)
                    if name == "gpu_exec":
                        self.adapter_owners[key] = ctx.run_id
                    elif name == "gpu_release" and self.adapter_owners.get(key) not in {
                        None,
                        ctx.run_id,
                    }:
                        raise ValueError(
                            "Only the original allocation owner may release it"
                        )
                    selected = next(t for t in current.tools() if t.name == name)
                    return await selected.call(arguments)

                tools.append(replace(tool, call=call))
        return NativeGroupServices(
            ctx,
            config,
            self.journal,
            resource_tools=tools,
            native_factory=self.native_factory,
            resource_lock=self.resource_lock,
        )

    def _save(self):
        write_json(self.root / "routing.json", self.routing)
        self.state.update(
            iteration=self.routing["iteration"], config_revision=self.config["revision"]
        )
        record_status(self.root, self.state)

    def _request(self):
        identity = self.routing["active_input"]
        if identity is None:
            return None
        row = get_intervention(self.root, identity)
        return {"id": identity, **row["request"]["payload"]}

    def _context(self):
        self.context = self.workflow.context(
            self.root,
            self.config,
            self.routing["iteration"],
            self.state.get("result"),
            self._request(),
        )
        directory = self.root / "inputs/contexts"
        directory.mkdir(parents=True, exist_ok=True)
        self.context_ref = directory / f"{uuid4().hex}.json"
        write_json(self.context_ref, self.context)
        write_json(
            self.root / "call.json",
            {
                "input": self.context,
                "config_revision": self.config["revision"],
                "policy": self.workflow.router_policy,
            },
        )

    def view(self):
        return {
            "root": self.root,
            "config": self.config,
            "iteration": self.routing["iteration"],
            "runs": self.runtime.observe()["runs"],
            "admitted": self.routing["admitted"],
            "input": self.context,
            "request": self._request(),
        }

    def _unknown(self):
        return bool(self.state.get("cleanup_errors")) or any(
            row["status"] == "needs-reconciliation"
            for row in self.runtime.observe()["runs"]
        )

    def _signature(self):
        observed = self.runtime.observe()
        return (
            self.generation,
            self.config["revision"],
            observed["paused"],
            tuple(observed["paused_runs"]),
            tuple((r["run_id"], r["round"], r["status"]) for r in observed["runs"]),
        )

    def _open_channel(self):
        # The path is short even when a run directory has a long project name.
        directory = Path(tempfile.mkdtemp(prefix="research-loop-control-"))
        directory.chmod(0o700)
        self.address = directory / "wake.sock"
        self.channel = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.channel.setblocking(False)
        self.channel.bind(str(self.address))
        self.address.chmod(0o600)
        loop = asyncio.get_running_loop()

        def receive():
            try:
                self.channel.recv(65536)
            except BlockingIOError:
                return
            self.wake.set()

        loop.add_reader(self.channel.fileno(), receive)
        write_json(
            self.root / "controller.json",
            {
                "pid": os.getpid(),
                "socket": str(self.address),
                "started_at": now(),
                "revision": self.config["revision"],
            },
        )

    async def _wait(self, task=None):
        wake = asyncio.create_task(self.wake.wait())
        event = asyncio.create_task(self.runtime.wait())
        waiting = {wake, event}
        if task is not None:
            waiting.add(task)
        done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        for future in (wake, event):
            if future not in done:
                future.cancel()
        await asyncio.gather(wake, event, return_exceptions=True)
        if wake in done:
            self.wake.clear()
        # Apply durable requests even when the wake datagram preceded the reader.
        await self._controls()
        return task is not None and task in done

    def _recover_receipts(self):
        # These are the input records needed for recovery, not an artifact audit.
        for run in self.runtime.observe()["runs"]:
            for number in range(1, run["round"] + 1):
                path = (
                    self.root
                    / "group-runs"
                    / run["run_id"]
                    / "rounds"
                    / f"{number:06d}"
                    / "input-receipt.json"
                )
                if not path.exists():
                    continue
                receipt = json.loads(path.read_text())
                for identity in receipt["interventions"]:
                    row = get_intervention(self.root, identity)
                    if row["status"] in {"received", "pending"}:
                        record_intervention(
                            self.root,
                            identity,
                            "applied",
                            input_receipt=str(path),
                            consumed_by=run["run_id"],
                            config_revision=run["config_revision"],
                        )

    def _ack_inputs(self, run_id, identities):
        record = self.runtime.observe(run_id)
        receipt = (
            self.root
            / "group-runs"
            / run_id
            / "rounds"
            / f"{record['round']:06d}"
            / "input-receipt.json"
        )
        for identity in identities:
            record_intervention(
                self.root,
                identity,
                "applied",
                input_receipt=str(receipt),
                consumed_by=run_id,
                config_revision=record["config_revision"],
            )

    async def _stop_targets(self, targets=None):
        active = [
            row
            for row in self.runtime.observe()["runs"]
            if row["status"] in {"running", "held", "returned"}
            and (targets is None or row["run_id"] in targets)
        ]
        # Admission closes on every target before any remote cleanup can wait.
        for row in active:
            ctx = self.runtime._contexts.get(row["run_id"])
            if ctx is not None and ctx.services is not None:
                ctx.services.close_admission()
        jobs = (
            [asyncio.create_task(self.runtime.stop())]
            if targets is None
            else [
                asyncio.create_task(self.runtime.stop(identity)) for identity in targets
            ]
        )
        # The serialized backend's current physical owner is distinct from
        # Groups that merely share its configuration or are waiting for a lease.
        selected = {row["run_id"] for row in active}
        for key in list(self.adapters):
            owner = self.adapter_owners.get(key)
            owner_active = (
                owner is not None and self.runtime.observe(owner)["status"] == "running"
            )
            if targets is None or owner in selected or not owner_active:
                jobs.append(self._close_adapter(key))
        if targets is None:
            jobs.append(asyncio.create_task(self.router.stop()))
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        errors = [str(value) for value in outcomes if isinstance(value, BaseException)]
        for value in outcomes:
            if isinstance(value, dict) and "reservations" in value:
                errors.extend(value.get("cleanup_errors", []))
                if any(row["status"] != "TERMINAL" for row in value["reservations"]):
                    errors.append("Resource reservation remains unsettled")
        if errors:
            self.state["cleanup_errors"] = errors
        for row in active:
            settled = self.runtime.observe(row["run_id"])
            if settled["status"] == "needs-reconciliation":
                continue
            workflow = self._workflow(settled["config_revision"])
            recover = getattr(workflow, "recover", None)
            if recover is not None:
                bundle = recover(self.view(), settled)
                if bundle:
                    descriptor = {
                        "predecessor": settled["run_id"],
                        "group_id": settled["group_id"],
                        "task_ref": settled["task_ref"],
                        "reference": bundle["descriptor_ref"],
                        "payload": bundle,
                    }
                    if not any(
                        value["predecessor"] == settled["run_id"]
                        for value in self.routing["recoveries"]
                    ):
                        self.routing["recoveries"].append(descriptor)
        self._save()

    def _affected(self, new, declared):
        active = {
            r["run_id"]: r
            for r in self.runtime.observe()["runs"]
            if r["status"] not in {"closed", "failed"}
        }
        if declared == ["all"]:
            chosen = set(active)
        else:
            if not declared or any(
                identity not in self.runtime._records for identity in declared
            ):
                raise ValueError("Revision requires explicit affected Run IDs or 'all'")
            chosen = set(declared)
        old = self.config
        shared = (
            old["workflow"]["settings"] != new["workflow"]["settings"]
            or Path(old["workflow"]["program"]).read_bytes()
            != Path(new["workflow"]["program"]).read_bytes()
            or old.get("resource") != new.get("resource")
            or old.get("environment") != new.get("environment")
        )
        required = set(active) if shared else set()
        old_groups = {g["id"]: g for g in old["groups"]}
        new_groups = {g["id"]: g for g in new["groups"]}
        for identity, record in active.items():
            before, after = (
                old_groups.get(record["group_id"]),
                new_groups.get(record["group_id"]),
            )
            changed = before is None or after is None
            if not changed:
                changed = (
                    before["agents"] != after["agents"]
                    or before["settings"] != after["settings"]
                    or Path(before["program"]).read_bytes()
                    != Path(after["program"]).read_bytes()
                )
                for agent_id in before["agents"].values():
                    a, b = old["agents"].get(agent_id), new["agents"].get(agent_id)
                    changed |= a != b or (
                        a is not None
                        and b is not None
                        and old["profiles"][a["profile"]]
                        != new["profiles"][b["profile"]]
                    )
            if changed:
                required.add(identity)
        changed = True
        while changed:
            expanded = chosen | {
                identity
                for identity, row in active.items()
                if set(row.get("dependencies", [])) & chosen
            }
            changed = expanded != chosen
            chosen = expanded
        if not required <= chosen:
            raise ValueError(
                "Revision affects additional active executions; declare their scope"
            )
        return chosen

    async def _controls(self):
        rows = list(pending_interventions(self.root))
        rows.sort(
            key=lambda row: (
                0
                if row["request"]["kind"] in {"stop", "pause", "continue", "revise"}
                else 1,
                row["received_at"],
            )
        )
        for row in rows:
            request, identity = row["request"], row["id"]
            kind, target = request["kind"], request["target"]
            try:
                target_revision = self.config["revision"]
                if target != "run":
                    record = self.runtime.observe(target)
                    target_revision = record["config_revision"]
                    if record["status"] in {"closed", "failed", "needs-reconciliation"}:
                        raise ValueError(
                            "Intervention target has ended or needs reconciliation"
                        )
                if request["expected_revision"] != target_revision:
                    raise ValueError(
                        "Configuration revision changed before this intervention"
                    )
                if self.committing and self.state["status"] == "completed":
                    raise ValueError(
                        "Run completed before this request could be accepted"
                    )
                if kind == "pause":
                    self.runtime.pause(None if target == "run" else target)
                    running = any(
                        r["status"] == "running"
                        and (target == "run" or r["run_id"] == target)
                        for r in self.runtime.observe()["runs"]
                    )
                    record_intervention(
                        self.root,
                        identity,
                        "pending" if running else "applied",
                        control_state="pausing" if running else "paused",
                    )
                    if target == "run":
                        self.state["status"] = "pausing" if running else "paused"
                elif kind == "continue":
                    for previous in rows:
                        prior = previous["request"]
                        if (
                            prior["kind"] == "pause"
                            and prior["target"] == target
                            and previous["received_at"] <= row["received_at"]
                            and get_intervention(self.root, previous["id"])["status"]
                            == "pending"
                        ):
                            record_intervention(
                                self.root,
                                previous["id"],
                                "applied",
                                control_state="superseded",
                                superseded_by=identity,
                            )
                    self.runtime.pause(
                        None if target == "run" else target, paused=False
                    )
                    record_intervention(
                        self.root, identity, "applied", control_state="continued"
                    )
                    self.state["status"] = "running"
                elif kind == "stop":
                    if target == "run":
                        (self.root / "STOP").touch()
                        self.stopping = True
                    record_intervention(
                        self.root, identity, "pending", control_state="stopping"
                    )
                    await self._stop_targets(None if target == "run" else {target})
                    unknown = self._unknown() or bool(self.state.get("cleanup_errors"))
                    record_intervention(
                        self.root,
                        identity,
                        "applied",
                        control_state="needs-reconciliation" if unknown else "stopped",
                        settled=not unknown,
                    )
                    if target == "run":
                        self.state["status"] = (
                            "needs-reconciliation" if unknown else "stopped"
                        )
                elif kind == "revise":
                    if self.committing:
                        record_intervention(
                            self.root,
                            identity,
                            "pending",
                            acceptance="publication-boundary",
                        )
                        continue
                    if target != "run":
                        raise ValueError(
                            "A revision targets the run and explicitly names affected executions"
                        )
                    new = self._config(request["payload"]["revision"])
                    try:
                        new_workflow = self._workflow(new["revision"])
                    except Exception as error:
                        raise ValueError(
                            f"Invalid workflow revision: {error}"
                        ) from error
                    affected = self._affected(
                        new, request["payload"].get("affected", [])
                    )
                    record_intervention(
                        self.root, identity, "pending", affected=sorted(affected)
                    )
                    await self._stop_targets(affected)
                    if self.state.get("cleanup_errors") or any(
                        self.runtime.observe(i)["status"] == "needs-reconciliation"
                        for i in affected
                    ):
                        raise ValueError(
                            "Affected execution remains unknown; revision was not activated"
                        )
                    write_json(
                        self.root / "active.json",
                        {"revision": new["revision"], "intervention": identity},
                    )
                    self.config, self.workflow = new, new_workflow
                    self.runtime.replace_definitions(
                        self._definitions(), new["revision"]
                    )
                    self.generation += 1
                    self._context()
                    record_intervention(
                        self.root,
                        identity,
                        "applied",
                        config_revision=new["revision"],
                        affected=sorted(affected),
                    )
                elif (
                    not self.stopping
                    and not self.committing
                    and not self.runtime.observe()["paused"]
                ):
                    normalized = self.workflow.validate_input(kind, request["payload"])
                    if target == "run":
                        if self.routing["active_input"] is None:
                            self.routing["active_input"] = identity
                            record_intervention(
                                self.root,
                                identity,
                                "pending",
                                acceptance="next-admission",
                                normalized=normalized,
                            )
                            self.generation += 1
                            self._context()
                    elif (
                        self.runtime.observe(target)["status"] == "held"
                        and target not in self.runtime.observe()["paused_runs"]
                    ):
                        refs = request["payload"].get("input_refs", [])
                        await self.runtime.resume(
                            target,
                            refs,
                            request["payload"].get("text", ""),
                            interventions=[identity],
                        )
                        self._ack_inputs(target, [identity])
                    else:
                        record_intervention(
                            self.root, identity, "pending", acceptance="target-boundary"
                        )
                self._save()
            except (ValueError, KeyError, FileNotFoundError) as error:
                record_intervention(self.root, identity, "rejected", reason=str(error))
                self.journal.emit(
                    "intervention.rejected", intervention_id=identity, reason=str(error)
                )

    async def _dispatch(self, decision):
        action = decision["action"]
        if action == "start":
            group = decision["group_id"]
            task_ref = str(Path(decision["task_ref"]).resolve())
            refs = decision.get("input_refs", [])
            predecessor, recovery = None, None
            old = [
                r
                for r in self.runtime.observe()["runs"]
                if r["group_id"] == group
                and r["task_ref"] == task_ref
                and r.get("interrupted")
            ]
            if old:
                predecessor = old[-1]["run_id"]
                descriptor = next(
                    (
                        r
                        for r in self.routing["recoveries"]
                        if r["predecessor"] == predecessor
                    ),
                    None,
                )
                recovery = descriptor["payload"] if descriptor else None
                state_path = self.root / "group-runs" / predecessor / "state.json"
                previous_state = (
                    json.loads(state_path.read_text()) if state_path.exists() else {}
                )
                if previous_state.get("executions") and recovery is None:
                    raise ValueError(
                        "Original command receipts require an explicit recovery consumer; the task cannot be executed again"
                    )
                if descriptor and descriptor["reference"] not in refs:
                    refs = [*refs, descriptor["reference"]]
            key = [group, task_ref, predecessor]
            if key in self.routing["tasks"]:
                raise ValueError(
                    "Duplicate Group dispatch without new input; reuse its completed evidence"
                )
            identities = []
            active_input = self.routing["active_input"]
            if active_input and get_intervention(self.root, active_input)["status"] in {
                "received",
                "pending",
            }:
                identities.append(active_input)
            run_id = await self.runtime.start(
                group,
                task_ref,
                refs,
                decision.get("prompt", ""),
                context=self.context,
                predecessor=predecessor,
                interventions=identities,
                recovery=recovery,
            )
            self.routing["admitted"].append(run_id)
            self.routing["tasks"].append(key)
            self._ack_inputs(run_id, identities)
        elif action == "resume":
            run_id = decision["run_id"]
            await self.runtime.resume(
                run_id, decision.get("input_refs", []), decision.get("prompt", "")
            )
            if run_id not in self.routing["admitted"]:
                self.routing["admitted"].append(run_id)
        elif action in {"hold", "close"}:
            getattr(self.runtime, action)(decision["run_id"])
        elif action == "wait":
            if not any(
                r["status"] == "running" for r in self.runtime.observe()["runs"]
            ):
                raise ValueError("Workflow cannot wait without an active execution")
            await self._wait()
        elif action == "finish_round":
            if self._unknown():
                raise RuntimeError(
                    "Publication requires settled operations and resources"
                )
            if any(
                r["status"] not in {"closed", "failed"}
                and r["run_id"] in self.routing["admitted"]
                for r in self.runtime.observe()["runs"]
            ):
                raise ValueError("Publication requires all admitted work to settle")
            proposal = self.workflow.complete(self.view(), decision["result_ref"])
            if proposal.get("action") not in {"continue", "stop"}:
                raise ValueError("Workflow completion must choose continue or stop")
            self.committing = True
            try:
                for key in list(self.adapters):
                    cleanup = self._close_adapter(key)
                    while not cleanup.done():
                        await self._wait(cleanup)
                    resource = cleanup.result()
                    if resource.get("cleanup_errors") or any(
                        r["status"] != "TERMINAL" for r in resource["reservations"]
                    ):
                        self.state["cleanup_errors"] = resource.get(
                            "cleanup_errors", []
                        ) or ["Resource reservation remains unsettled"]
                        raise RuntimeError("Publication requires settled resources")
                self.adapters.clear()
                baseline = PublicationStore(self.root).prepare()
                commit = asyncio.create_task(
                    publication.commit(self.root, self.state, baseline, proposal)
                )
                while not commit.done():
                    await self._wait(commit)
                commit.result()
            finally:
                self.committing = False
            outcome = proposal.get("input_outcome")
            if (
                outcome
                and outcome["id"] == self.routing["active_input"]
                and outcome["status"] == "completed"
            ):
                self.routing["active_input"] = None
            if proposal["action"] == "stop":
                for row in pending_interventions(self.root):
                    record_intervention(
                        self.root,
                        row["id"],
                        "rejected",
                        reason="Run completed before this request could be accepted",
                    )
                    if row["id"] == self.routing["active_input"]:
                        self.routing["active_input"] = None
                self._save()
                return True
            self.routing.update(
                iteration=self.routing["iteration"] + 1, admitted=[], tasks=[]
            )
            self._context()
        elif action == "escalate":
            raise RuntimeError(
                "Workflow needs input: " + decision.get("reason", "unspecified blocker")
            )
        else:
            raise ValueError(f"Unknown workflow action: {action}")
        self._save()
        return False

    async def run(self, *, resume=False):
        loop = asyncio.get_running_loop()
        installed_signals = []
        try:
            self.state = await publication.recover(self.root, self.state)
            if (
                self.routing["active_input"]
                and get_intervention(self.root, self.routing["active_input"])["status"]
                == "rejected"
            ):
                self.routing["active_input"] = None
            published = PublicationStore(self.root).published_state()
            if published:
                outcome = published.get("input_outcome")
                if (
                    outcome
                    and outcome["id"] == self.routing["active_input"]
                    and outcome["status"] == "completed"
                ):
                    self.routing["active_input"] = None
                if (
                    published["status"] == "iteration-completed"
                    and self.routing["iteration"] <= published["iteration"]
                ):
                    self.routing.update(
                        iteration=published["next_iteration"], admitted=[], tasks=[]
                    )
            if self._unknown():
                self.state["status"] = "needs-reconciliation"
                self._save()
                raise RuntimeError(
                    "Unknown operations require reconciliation; no work was dispatched"
                )
            if (self.root / "STOP").exists():
                if not resume:
                    if self.state["status"] != "completed":
                        self.state["status"] = "stopped"
                    return self.state
                (self.root / "STOP").unlink()
                self.runtime._stopped = False
            if self.state["status"] == "completed":
                if not resume:
                    return self.state
                self.routing.update(
                    iteration=self.state["iteration"] + 1, admitted=[], tasks=[]
                )
            if resume:
                self.state.pop("error", None)
                self.state.pop("ended_at", None)
            self._recover_receipts()
            self._context()
            self._open_channel()

            def terminate():
                (self.root / "STOP").touch()
                self.stopping = True
                self.wake.set()

            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, terminate)
                installed_signals.append(signum)
            self.state.update(status="running", started_at=now(), pid=os.getpid())
            self._save()
            while not self.stopping:
                await self._controls()
                if self.stopping:
                    break
                if (self.root / "STOP").exists():
                    self.stopping = True
                    break
                observed = self.runtime.observe()
                if self._unknown():
                    raise RuntimeError(
                        "Execution result is unknown; reconciliation required"
                    )
                active = [r for r in observed["runs"] if r["status"] == "running"]
                if observed["paused"]:
                    self.state["status"] = "pausing" if active else "paused"
                    self._save()
                    await self._wait()
                    continue
                self.state["status"] = "running"
                decision = self.workflow.advance(self.view())
                if decision is not None:
                    if await self._dispatch(decision):
                        return self.state
                    continue
                if len(active) >= self.workflow.max_active_groups:
                    await self._wait()
                    continue
                value = self.workflow.route(
                    packet(
                        self.root,
                        self.config,
                        observed["runs"],
                        self.routing["admitted"],
                        self.context,
                        request=self._request(),
                        recoveries=self.routing["recoveries"],
                        context_ref=self.context_ref,
                    )
                )
                if not value["allowed_actions"]:
                    if active:
                        await self._wait()
                        continue
                    raise RuntimeError(
                        "Workflow has no admissible work or event to wait for"
                    )
                signature = self._signature()
                task = asyncio.create_task(
                    self.router.decide(
                        self.config,
                        self.workflow.router_policy,
                        value,
                        iteration=self.routing["iteration"],
                    )
                )
                try:
                    while (
                        not task.done()
                        and not self.stopping
                        and signature == self._signature()
                    ):
                        await self._wait(task)
                    if self.stopping or signature != self._signature():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        self.journal.emit(
                            "router.discarded",
                            reason="execution or configuration boundary changed",
                        )
                        continue
                    decision = task.result()
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                decision = {
                    **decision,
                    "task_ref": value["references"].get(decision["task_ref"], ""),
                    "result_ref": value["references"].get(decision["result_ref"], ""),
                    "input_refs": [
                        value["references"][ref] for ref in decision["input_refs"]
                    ],
                }
                if await self._dispatch(decision):
                    return self.state
            await self._stop_targets()
            self.state["status"] = (
                "needs-reconciliation"
                if self._unknown() or self.state.get("cleanup_errors")
                else "stopped"
            )
            return self.state
        except PublicationStopped:
            await self._stop_targets()
            self.state["status"] = (
                "needs-reconciliation" if self._unknown() else "stopped"
            )
            return self.state
        except BaseException as error:
            await self._stop_targets()
            self.state.update(
                status="needs-reconciliation"
                if self._unknown() or self.state.get("cleanup_errors")
                else "failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            self.journal.emit("run.error", error=self.state["error"])
            raise
        finally:
            self.state["ended_at"] = now()
            try:
                try:
                    # Finalization records the computed state, including an early
                    # publication recovery; it must not derive its iteration
                    # again from potentially stale routing metadata.
                    write_json(self.root / "routing.json", self.routing)
                    record_status(self.root, self.state)
                finally:
                    write_json(
                        self.root / "completion.json",
                        {
                            "run_id": json.loads((self.root / "run.json").read_text())[
                                "id"
                            ],
                            "status": self.state["status"],
                            "ended_at": self.state["ended_at"],
                            "error": self.state.get("error"),
                        },
                    )
            finally:
                try:
                    for signum in installed_signals:
                        loop.remove_signal_handler(signum)
                    if self.channel is not None:
                        loop.remove_reader(self.channel.fileno())
                        self.channel.close()
                    if self.address is not None:
                        self.address.unlink(missing_ok=True)
                        self.address.parent.rmdir()
                finally:
                    self.journal.close()


async def run_foreground(root, *, resume=False, native_factory=Native):
    root = Path(root).resolve(strict=True)
    with writer(root):
        controller = Controller(root, native_factory=native_factory)
        return await controller.run(resume=resume)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    identity = json.loads((args.root / "implementation/identity.json").read_text())
    if not Path(__file__).resolve().is_relative_to(Path(identity["source"]).resolve()):
        raise RuntimeError(
            "Controller must execute its prepared implementation; use application.start"
        )
    try:
        asyncio.run(run_foreground(args.root, resume=args.resume))
    except Exception as error:
        print(
            json.dumps({"error": type(error).__name__, "message": str(error)}),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
