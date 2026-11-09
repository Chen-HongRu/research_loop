"""Local subprocess SSH doubles exercise resource effects without remote access."""

import asyncio
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop.slurm import Slurm

# This program implements only scheduler I/O, with persistent state shared across
# independent fake SSH processes. salloc still runs the adapter's real holder.
FAKE_SSH = r"""
import json, os, shlex, sys, time
from pathlib import Path
root = Path(sys.argv[1])
argv = shlex.split(sys.argv[2])
path = root / "scheduler.json"
state = json.loads(path.read_text()) if path.exists() else {"jobs": [], "mode": "normal"}
def save():
    path.write_text(json.dumps(state))
command = argv[0]
if command == "salloc":
    if state["mode"] == "banner_timeout":
        print("Connection timed out during banner exchange")
        print("Connection to UNKNOWN port 65535 timed out")
        sys.exit(255)
    name = next(x.split("=", 1)[1] for x in argv if x.startswith("--job-name="))
    job = {"id": str(100 + len(state["jobs"])), "name": name, "state": "RUNNING", "elapsed": 7, "gpu": "gres/gpu=1"}
    state["jobs"].append(job)
    save()
    if state["mode"] == "ack_loss":
        sys.exit(255)
    if state["mode"] == "delay_ack":
        while not (root / "ack_ready").exists():
            time.sleep(0.01)
    env = dict(os.environ, SLURM_JOB_ID=job["id"])
    index = argv.index("bash")
    os.execvpe("bash", argv[index:], env)
elif command == "srun":
    if state["mode"] == "step_loss":
        with (root / "effects").open("a") as stream:
            stream.write("effect\n")
        sys.exit(255)
    index = argv.index("bash")
    os.execvp("bash", argv[index:])
elif command == "squeue":
    name = argv[argv.index("--name") + 1]
    for job in state["jobs"]:
        if job["name"] == name and job["state"] == "RUNNING":
            print(job["id"] + "|" + name + "|RUNNING")
elif command == "scancel":
    for job in state["jobs"]:
        if job["id"] == argv[1]:
            job["state"] = "CANCELLED"
    save()
elif command == "sacct":
    if state["mode"] == "no_accounting":
        sys.exit(0)
    for job in state["jobs"]:
        if ("-j" in argv and job["id"] != argv[argv.index("-j") + 1]) or ("--name" in argv and job["name"] != argv[argv.index("--name") + 1]):
            continue
        print("|".join((job["id"], job["name"], job["state"], str(job["elapsed"]), job["gpu"])))
else:
    raise RuntimeError("unexpected scheduler command: " + command)
"""


class SlurmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = self.root / "fake_ssh.py"
        self.script.write_text(FAKE_SSH)
        self.config = {
            "host": "cluster-example",
            "partition": "debug",
            "remote_root": str(self.root),
            "ledger_path": str(self.root / "budget.json"),
            "gpu_seconds_total": 600,
            "job_seconds": 120,
        }
        self.adapters = []
        self.calls = []
        create = asyncio.create_subprocess_exec

        async def spawn(*argv, **kwargs):
            self.assertEqual(argv[0], "ssh")
            self.calls.append(shlex.split(argv[-1]))
            return await create(
                sys.executable, str(self.script), str(self.root), argv[-1], **kwargs
            )

        self.spawn_patch = patch(
            "research_loop.slurm.asyncio.create_subprocess_exec", spawn
        )
        self.spawn_patch.start()

    async def asyncTearDown(self):
        try:
            for adapter in self.adapters:
                await adapter.close()
        finally:
            self.spawn_patch.stop()
            self.temporary.cleanup()

    def adapter(self):
        adapter = Slurm(self.config, self.root / ("run-" + str(len(self.adapters))))
        self.adapters.append(adapter)
        return adapter

    def scheduler(self, **changes):
        path = self.root / "scheduler.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"jobs": [], "mode": "normal"}
        )
        state.update(changes)
        path.write_text(json.dumps(state))
        return state

    async def invoke(self, adapter, name, **arguments):
        tool = next(tool for tool in adapter.tools() if tool.name == name)
        return await tool.call(arguments)

    async def test_real_holder_original_job_execution_and_exact_settlement(self):
        adapter = self.adapter()
        result = await self.invoke(adapter, "gpu_exec", command="printf 'hello\\n'")
        self.assertFalse(result["running"])
        self.assertEqual(result["output"], "hello\n")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(Path(result["log_path"]).read_text(), "hello\n")
        reservation = adapter.status()["reservations"][0]
        allocation = next(argv for argv in self.calls if argv[0] == "salloc")
        step = next(argv for argv in self.calls if argv[0] == "srun")
        self.assertIn("--gres=gpu:1", allocation)
        self.assertIn("--time=2", allocation)
        self.assertEqual(step[step.index("--jobid") + 1], reservation["job_id"])
        receipt = await self.invoke(adapter, "gpu_release")
        self.assertEqual(receipt["state"], "CANCELLED")
        self.assertEqual(receipt["elapsed_seconds"], 7)
        self.assertEqual(adapter.status()["budget"]["used_seconds"], 7)
        await self.invoke(adapter, "gpu_exec", command="printf second")
        result = await adapter.close()
        self.assertEqual(result["budget"]["used_seconds"], 14)
        self.assertEqual(await adapter.close(), result)
        self.assertEqual(len(self.scheduler()["jobs"]), 2)

    async def test_ack_loss_never_retries_and_reconciles_original_only(self):
        self.scheduler(mode="ack_loss")
        adapter = self.adapter()
        with self.assertRaisesRegex(RuntimeError, "original reservation .*gpu_status"):
            await self.invoke(adapter, "gpu_exec", command="printf must-not-run")
        path = Path(self.config["ledger_path"])
        before = path.read_bytes(), path.stat().st_mtime_ns, len(self.calls)
        status = await self.invoke(adapter, "gpu_status")
        self.assertEqual(
            before, (path.read_bytes(), path.stat().st_mtime_ns, len(self.calls))
        )
        entry = status["reservations"][0]
        self.assertEqual(entry["status"], "UNKNOWN")
        self.assertEqual(adapter.status()["budget"]["reserved_seconds"], 150)
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="printf must-not-run")
        with self.assertRaises(RuntimeError):
            await self.invoke(
                adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
            )
        self.assertEqual(len(self.scheduler()["jobs"]), 1)
        state = self.scheduler()
        state["jobs"][0]["state"] = "TIMEOUT"
        self.scheduler(**state)
        receipt = await self.invoke(
            adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
        )
        self.assertEqual(receipt["job_id"], "100")
        self.assertEqual(receipt["elapsed_seconds"], 7)
        self.assertEqual(len(self.scheduler()["jobs"]), 1)
        self.scheduler(mode="normal")
        await self.invoke(adapter, "gpu_exec", command="printf explicit-next-command")
        self.assertEqual(len(self.scheduler()["jobs"]), 2)

    async def test_banner_timeout_reconciles_original_without_a_second_dispatch(self):
        self.scheduler(mode="banner_timeout")
        adapter = self.adapter()
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="printf must-not-run")
        entry = adapter.status()["reservations"][0]
        self.assertEqual(entry["status"], "UNKNOWN")
        # Preserve the pre-fix UNKNOWN receipt, then recover in a fresh process-like
        # adapter using the original protected log and ledger.
        with patch.object(
            adapter, "_observe", side_effect=RuntimeError("old unresolved dispatch")
        ):
            await adapter.close()
        adapter = Slurm(self.config, adapter.root)
        self.adapters.append(adapter)
        receipt = await self.invoke(
            adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
        )
        self.assertEqual(receipt["state"], "NOT_DISPATCHED")
        self.assertEqual(receipt["elapsed_seconds"], 0)
        self.assertEqual(adapter.status()["budget"]["reserved_seconds"], 0)
        self.assertEqual(adapter.status()["budget"]["used_seconds"], 0)
        self.assertEqual(sum(call[0] == "salloc" for call in self.calls), 1)
        self.assertEqual(self.scheduler()["jobs"], [])

    async def test_missing_accounting_or_mixed_output_does_not_prove_non_dispatch(self):
        self.scheduler(mode="banner_timeout")
        adapter = self.adapter()
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="printf must-not-run")
        entry = adapter.status()["reservations"][0]
        log = adapter.root / (entry["reservation_id"] + ".allocator.log")
        diagnostic = log.read_bytes()
        for output in (b"", b"salloc: submitting job\n" + diagnostic, b"x" * 4097):
            log.write_bytes(output)
            with self.assertRaisesRegex(RuntimeError, "accounting unavailable"):
                await self.invoke(
                    adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
                )
            self.assertEqual(adapter.status()["reservations"][0]["status"], "UNKNOWN")

    async def test_status_before_allocation_has_no_side_effects(self):
        adapter = self.adapter()
        before = set(self.root.iterdir())
        status = await self.invoke(adapter, "gpu_status")
        self.assertEqual(status, adapter.status())
        self.assertEqual(status["reservations"], [])
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse(self.calls)

    async def test_step_transport_loss_has_no_second_effect(self):
        self.scheduler(mode="step_loss")
        adapter = self.adapter()
        result = await self.invoke(
            adapter, "gpu_exec", command="arbitrary user command"
        )
        self.assertEqual(result["exit_code"], 255)
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="arbitrary user command")
        self.assertEqual((self.root / "effects").read_text(), "effect\n")
        self.assertEqual(len(self.scheduler()["jobs"]), 1)
        receipt = await self.invoke(adapter, "gpu_release")
        self.assertEqual(receipt["elapsed_seconds"], 7)
        self.assertFalse(adapter.status()["budget"]["continuation_blocked"])

    async def test_ledger_lock_cross_attempt_spending_and_missing_ledger(self):
        first, second = self.adapter(), self.adapter()
        first._open()
        with self.assertRaises(BlockingIOError):
            second._open()
        await first.close()
        await self.invoke(second, "gpu_exec", command="true")
        await second.close()
        third = self.adapter()
        third._open()
        self.assertEqual(third.status()["budget"]["used_seconds"], 7)
        await third.close()
        Path(self.config["ledger_path"]).unlink()
        fourth = Slurm(self.config, self.root)
        with self.assertRaisesRegex(ValueError, "refusing budget reset"):
            fourth._open()
        with self.assertRaisesRegex(ValueError, "refusing budget reset"):
            fourth.status()

    async def test_unknown_existing_ledger_is_not_reset_or_overwritten(self):
        entry = {
            "reservation_id": "previous",
            "job_name": "rl-previous",
            "reserved_seconds": 150,
            "status": "UNKNOWN",
            "continuation_authorized": False,
        }
        path = Path(self.config["ledger_path"])
        path.write_text(
            json.dumps(
                {"schema_version": 1, "authorized_seconds": 600, "entries": [entry]}
            )
        )
        adapter = self.adapter()
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="true")
        self.assertEqual(adapter.status()["reservations"], [entry])
        self.assertFalse(self.calls)
        snapshot = adapter.status()
        snapshot["reservations"][0]["status"] = "TERMINAL"
        self.assertEqual(adapter.status()["reservations"][0]["status"], "UNKNOWN")

    async def test_missing_accounting_does_not_free_reservation(self):
        adapter = self.adapter()
        await self.invoke(adapter, "gpu_exec", command="true")
        self.scheduler(mode="no_accounting")
        receipt = await self.invoke(adapter, "gpu_release")
        self.assertEqual(receipt["state"], "UNKNOWN")
        self.assertEqual(adapter.status()["budget"]["reserved_seconds"], 150)
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="true")
        self.assertEqual(len(self.scheduler()["jobs"]), 1)

    async def test_interactive_poll_and_close_running_session(self):
        adapter = self.adapter()
        result = await self.invoke(
            adapter, "gpu_exec", command="read -r line; printf 'read:%s\\n' \"$line\""
        )
        self.assertTrue(result["running"])
        with self.assertRaisesRegex(RuntimeError, "unsettled"):
            await self.invoke(adapter, "gpu_release")
        result = await self.invoke(
            adapter, "gpu_poll", session_id=result["session_id"], chars="input\n"
        )
        self.assertFalse(result["running"])
        self.assertEqual(result["output"], "read:input\n")
        running = await self.invoke(adapter, "gpu_exec", command="read -r line")
        self.assertTrue(running["running"])
        result = await adapter.close()
        self.assertEqual(result["receipt"]["elapsed_seconds"], 7)
        self.assertTrue(all(s.done.is_set() for s in adapter._sessions.values()))
        self.assertIsNotNone(adapter._allocator.returncode)

    async def test_reserve_is_durable_before_dispatch_and_budget_bounds(self):
        adapter = self.adapter()
        original = asyncio.create_subprocess_exec

        async def checked(*argv, **kwargs):
            if shlex.split(argv[-1])[0] == "salloc":
                ledger = json.loads(Path(self.config["ledger_path"]).read_text())
                self.assertEqual(ledger["entries"][0]["status"], "RESERVED")
                self.assertEqual(ledger["entries"][0]["reserved_seconds"], 150)
            return await original(*argv, **kwargs)

        with patch("research_loop.slurm.asyncio.create_subprocess_exec", checked):
            await self.invoke(adapter, "gpu_exec", command="true")
        await adapter.close()
        self.config["gpu_seconds_total"] = 601
        with self.assertRaises(ValueError):
            Slurm(self.config, self.root)._open()
        self.config["gpu_seconds_total"] = 600
        self.config["job_seconds"] = 61
        with self.assertRaises(ValueError):
            Slurm(self.config, self.root)

    async def test_wrong_gpu_receipt_and_reservation_overrun_stay_blocked(self):
        adapter = self.adapter()
        await self.invoke(adapter, "gpu_exec", command="true")
        state = self.scheduler()
        state["jobs"][0]["gpu"] = "gres/gpu=2"
        self.scheduler(**state)
        self.assertEqual(
            (await self.invoke(adapter, "gpu_release"))["state"], "UNKNOWN"
        )
        self.assertEqual(adapter.status()["budget"]["reserved_seconds"], 150)
        entry = adapter.status()["reservations"][0]
        state = self.scheduler()
        state["jobs"][0].update(gpu="gres/gpu=1", elapsed=151)
        self.scheduler(**state)
        receipt = await self.invoke(
            adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
        )
        self.assertEqual(receipt["elapsed_seconds"], 151)
        self.assertTrue(adapter.status()["budget"]["continuation_blocked"])
        with self.assertRaises(RuntimeError):
            await self.invoke(adapter, "gpu_exec", command="true")
        self.assertEqual(len(self.scheduler()["jobs"]), 1)

    async def test_close_cancellation_still_finishes_cleanup(self):
        adapter = self.adapter()
        result = await self.invoke(adapter, "gpu_exec", command="read -r line")
        self.assertTrue(result["running"])
        closing = asyncio.create_task(adapter.close())
        await asyncio.sleep(0.01)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertTrue(adapter.status()["closed"])
        self.assertTrue(all(s.done.is_set() for s in adapter._sessions.values()))
        self.assertIsNotNone(adapter._allocator.returncode)
        self.assertEqual(adapter.status()["budget"]["used_seconds"], 7)

    async def test_terminal_reconcile_is_read_only_and_preserves_explicit_block(self):
        adapter = self.adapter()
        await self.invoke(adapter, "gpu_exec", command="true")
        receipt = await self.invoke(adapter, "gpu_release")
        entry = adapter._ledger["entries"][0]
        entry["continuation_authorized"] = False
        adapter._persist()
        path = Path(self.config["ledger_path"])
        before = path.read_bytes(), path.stat().st_mtime_ns, len(self.calls)
        returned = await self.invoke(
            adapter, "gpu_reconcile", reservation_id=entry["reservation_id"]
        )
        self.assertEqual(returned, receipt)
        self.assertEqual(
            before, (path.read_bytes(), path.stat().st_mtime_ns, len(self.calls))
        )
        self.assertTrue(adapter.status()["budget"]["continuation_blocked"])
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            await self.invoke(adapter, "gpu_exec", command="true")
        self.assertEqual(len(self.scheduler()["jobs"]), 1)

    async def test_stop_during_allocation_ack_prevents_step_and_cleans_holder(self):
        self.scheduler(mode="delay_ack")
        adapter = self.adapter()
        execution = asyncio.create_task(
            self.invoke(adapter, "gpu_exec", command="printf must-not-run")
        )
        for _ in range(200):
            if self.scheduler()["jobs"]:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("local allocator did not create the original job")
        entry = adapter.status()["reservations"][0]
        (adapter.root / "STOP").write_text("operator stop\n")
        (self.root / "ack_ready").touch()
        with self.assertRaisesRegex(
            RuntimeError, "STOP recorded before command dispatch"
        ):
            await execution
        self.assertEqual(len(self.scheduler()["jobs"]), 1)
        self.assertFalse(any(argv[0] == "srun" for argv in self.calls))
        self.assertIsNotNone(adapter._allocator.returncode)
        settled = adapter.status()["reservations"][0]
        self.assertEqual(settled["reservation_id"], entry["reservation_id"])
        self.assertEqual(settled["receipt"]["job_id"], "100")
        self.assertEqual(settled["receipt"]["elapsed_seconds"], 7)

    async def test_stop_before_allocation_dispatch_keeps_zero_effect_reservation(self):
        adapter = self.adapter()
        persist = adapter._persist

        def stop_after_reserve():
            persist()
            adapter.root.mkdir(parents=True, exist_ok=True)
            (adapter.root / "STOP").write_text("operator stop\n")

        with patch.object(adapter, "_persist", stop_after_reserve):
            with self.assertRaisesRegex(
                RuntimeError, "STOP recorded before allocation dispatch"
            ):
                await self.invoke(adapter, "gpu_exec", command="true")
        self.assertFalse(self.calls)
        reservation = adapter.status()["reservations"][0]
        self.assertEqual(reservation["status"], "TERMINAL")
        self.assertEqual(reservation["receipt"]["state"], "NOT_DISPATCHED")
        self.assertEqual(reservation["receipt"]["elapsed_seconds"], 0)

    async def test_session_log_failure_does_not_leave_local_process_alive(self):
        adapter = self.adapter()
        await self.invoke(adapter, "gpu_exec", command="true")
        original = Path.open

        def open_file(path, *args, **kwargs):
            if path.suffix == ".log" and ".allocator." not in path.name:
                raise OSError("synthetic log failure")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            result = await self.invoke(adapter, "gpu_exec", command="read -r line")
        self.assertFalse(result["running"])
        self.assertIn("synthetic log failure", result["error"])
        self.assertEqual(adapter.status()["reservations"][0]["status"], "UNKNOWN")
        receipt = await self.invoke(adapter, "gpu_release")
        self.assertEqual(receipt["elapsed_seconds"], 7)


if __name__ == "__main__":
    unittest.main()
