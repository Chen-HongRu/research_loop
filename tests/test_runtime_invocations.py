"""Real exec-based callbacks and durable local launch fencing, without SQLite."""

import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_loop.runtime.files import publish_artifact
from research_loop.runtime import invocations
from research_loop.runtime import worker_process
from research_loop.runtime.invocations import LocalProcessInvoker, _locked


MODULE = """
from pathlib import Path
import json
import os
import subprocess
import sys
import time

def invoke(kind, payload, config):
    print('A callback log is not its structured result')
    directory = Path(config['directory'])
    with (directory / 'calls').open('a') as stream:
        stream.write(payload['id'] + '\\n')
    (directory / ('ready-' + payload['id'])).write_text('ready')
    if payload.get('descendant'):
        code = '''
import json, os, signal, sys, time
from pathlib import Path
directory, name = Path(sys.argv[1]), sys.argv[2]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
ready = directory / ('descendant-' + name + '.json')
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({'pid':os.getpid(), 'group':os.getpgrp()}))
temporary.replace(ready)
gate = directory / ('after-completion-' + name)
deadline = time.monotonic() + 5
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if gate.exists():
    time.sleep(0.05)
    (directory / ('late-write-' + name)).write_text('late descendant effect')
'''
        subprocess.Popen([sys.executable, '-c', code, str(directory), payload['id']])
        deadline = time.monotonic() + 3
        while not (directory / ('descendant-' + payload['id'] + '.json')).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Descendant did not start')
            time.sleep(0.01)
    if payload.get('barrier'):
        deadline = time.monotonic() + 5
        while len(list(directory.glob('ready-*'))) < 2:
            if time.monotonic() >= deadline:
                raise TimeoutError('Callbacks did not overlap')
            time.sleep(0.01)
    if payload.get('sleep'):
        time.sleep(payload['sleep'])
    if payload.get('raise'):
        raise RuntimeError('synthetic failure ' + 'x' * 2000)
    return {'kind': kind, 'value': sum(payload.get('numbers', [])), 'marker': config['marker']}
"""


class RuntimeInvocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.module = self.root / "callback.py"
        self.module.write_text(MODULE)
        self.identity = {"id": "local-fixture", "version": "v1"}
        self.config = {"directory": str(self.root), "marker": "captured"}
        self.invoker = self.new_invoker()

    def new_invoker(self):
        return LocalProcessInvoker(self.root, self.module, self.identity, self.config)

    def input(self, identity="callback", **values):
        return publish_artifact(self.root, dict(id=identity, **values))

    def wait_for(self, token, *, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.invoker.inspect(token)
            if result["completion_ref"] is not None:
                return result
            time.sleep(0.02)
        self.fail(f"Invocation {token} did not publish completion")

    def ready(self, identity):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if (self.root / f"ready-{identity}").exists():
                return
            time.sleep(0.01)
        self.fail("Callback did not start")

    def completion(self, observed):
        self.assertEqual(observed["completion_ref"], observed["proof_ref"])
        value = json.loads((self.root / observed["completion_ref"]).read_text())
        self.assertEqual(
            set(value),
            {
                "version",
                "launch_token",
                "kind",
                "input_ref",
                "adapter",
                "status",
                "result",
                "error",
                "sender_retired",
                "exit_code",
            },
        )
        self.assertEqual(value["adapter"], self.identity)
        self.assertIs(value["sender_retired"], True)
        return value

    def test_completion_and_exactly_once_launch_survive_new_invoker(self):
        reference = self.input(numbers=[1, 2, 3])
        deadline = time.time() + 8
        self.invoker.launch("call-one", "agent", reference, deadline)
        self.invoker = self.new_invoker()
        completed = self.wait_for("call-one")
        value = self.completion(completed)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(
            value["result"], {"kind": "agent", "value": 6, "marker": "captured"}
        )
        self.assertEqual(value["exit_code"], 0)
        self.assertEqual(value["input_ref"], reference)
        self.assertIsNotNone(completed["worker_identity"])
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=AssertionError("Duplicate dispatched"),
        ):
            self.assertEqual(
                self.invoker.launch("call-one", "agent", reference, deadline), completed
            )
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["callback"])
        self.assertIn(
            "callback log",
            (self.root / "invocations/call-one/callback.log").read_text(),
        )
        self.assertFalse(list(self.root.glob("*.sqlite3")))

    def test_two_callbacks_overlap_without_blocking_launch(self):
        first = self.input("first", barrier=True, numbers=[1])
        second = self.input("second", barrier=True, numbers=[2])
        deadline = time.time() + 8
        self.invoker.launch("backend-first", "backend", first, deadline)
        self.invoker.launch("backend-second", "backend", second, deadline)
        for token, score in (("backend-first", 1), ("backend-second", 2)):
            self.assertEqual(
                self.completion(self.wait_for(token))["result"]["value"], score
            )
        self.assertEqual(
            set((self.root / "calls").read_text().splitlines()), {"first", "second"}
        )

    def test_callback_failure_has_bounded_error_and_exited_sender(self):
        self.invoker.launch(
            "call-failed", "agent", self.input(**{"raise": True}), time.time() + 8
        )
        value = self.completion(self.wait_for("call-failed"))
        self.assertEqual(value["status"], "failed")
        self.assertEqual(value["exit_code"], 1)
        self.assertIsNone(value["result"])
        self.assertLessEqual(len(value["error"]), 1000)
        self.assertIn("RuntimeError", value["error"])

    def test_deadline_terminates_owned_callback_group_and_publishes_proof(self):
        self.invoker.launch(
            "call-timeout", "agent", self.input("timeout", sleep=30), time.time() + 2
        )
        self.ready("timeout")
        value = self.completion(self.wait_for("call-timeout", timeout=5))
        self.assertEqual(value["status"], "timed_out")
        self.assertLess(value["exit_code"], 0)
        self.assertIsNone(value["result"])
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["timeout"])

    def test_same_group_descendant_cannot_write_after_any_completion(self):
        for outcome in ("completed", "failed", "timed_out", "cancelled"):
            with self.subTest(outcome=outcome):
                payload = dict(descendant=True)
                if outcome == "failed":
                    payload["raise"] = True
                if outcome in {"timed_out", "cancelled"}:
                    payload["sleep"] = 30
                token = "descendant-" + outcome
                deadline = time.time() + (1 if outcome == "timed_out" else 8)
                self.invoker.launch(
                    token, "agent", self.input(outcome, **payload), deadline
                )
                gate = self.root / ("after-completion-" + outcome)
                try:
                    ready = self.root / ("descendant-" + outcome + ".json")
                    started = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < started:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    identity = json.loads(ready.read_text())
                    self.assertNotEqual(identity["pid"], identity["group"])
                    if outcome == "cancelled":
                        self.invoker.cancel(token, "Synthetic descendant cancellation")
                    value = self.completion(self.wait_for(token))
                    self.assertEqual(value["status"], outcome)
                    if outcome == "completed":
                        self.assertEqual(value["exit_code"], 0)
                        self.assertEqual(value["result"]["marker"], "captured")
                    elif outcome == "failed":
                        self.assertEqual(value["exit_code"], 1)
                        self.assertIn("RuntimeError", value["error"])
                    else:
                        self.assertLess(value["exit_code"], 0)
                finally:
                    # If this regresses, release the bounded writer so it exits
                    # after its forbidden write instead of leaving a live child.
                    gate.touch()
                time.sleep(0.3)
                self.assertFalse((self.root / ("late-write-" + outcome)).exists())

    def test_unverifiable_group_cleanup_keeps_invocation_unknown(self):
        token = "unverified-cleanup"
        folder = invocations._ensure_folder(self.root, token)
        binding = dict(kind="agent", input_ref=self.input(), deadline=time.time() + 8)
        invocations.publish_json(
            folder / "claimed.json", self.invoker._claim(token, binding)
        )
        with (
            patch.object(
                worker_process,
                "_live_group_members",
                side_effect=OSError("Synthetic unavailable kernel metadata"),
            ),
            patch("sys.stderr", io.StringIO()),
        ):
            self.assertEqual(worker_process._supervise(self.root, token), 1)
        self.assertFalse((folder / "completion.json").exists())
        observed = self.invoker.inspect(token)
        self.assertEqual(observed["status"], "unknown")
        self.assertIsNone(observed["proof_ref"])

    def test_cancel_survives_new_invoker_and_needs_no_parent_pid_kill(self):
        binding = dict(
            kind="backend",
            input_ref=self.input("cancel", sleep=30),
            deadline=time.time() + 15,
        )
        self.invoker.launch("call-cancel", **binding)
        self.ready("cancel")
        self.invoker = self.new_invoker()
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=AssertionError("Active sender redispatched"),
        ):
            self.assertEqual(
                self.invoker.launch("call-cancel", **binding)["status"], "running"
            )
            observation = self.invoker.retire_unlaunched("call-cancel", binding)
            self.assertEqual(observation["status"], "running")
            self.assertIsNone(observation["proof_ref"])
        with patch(
            "research_loop.runtime.invocations.os.kill",
            side_effect=AssertionError("Parent PID kill"),
        ):
            self.invoker.cancel("call-cancel", "Operator requested cancellation")
            self.invoker.cancel("call-cancel", "Repeated request")
        value = self.completion(self.wait_for("call-cancel"))
        self.assertEqual(value["status"], "cancelled")
        self.assertLess(value["exit_code"], 0)
        self.assertEqual(
            json.loads((self.root / "invocations/call-cancel/cancel.json").read_text())[
                "reason"
            ],
            "Operator requested cancellation",
        )

    def test_retirement_fences_unlaunched_sender_and_replays_same_proof(self):
        reference = self.input()
        binding = dict(kind="agent", input_ref=reference, deadline=time.time() + 8)
        result = self.invoker.retire_unlaunched("call-retired", binding)
        value = self.completion(result)
        self.assertEqual(value["status"], "not_started")
        self.assertIsNone(value["exit_code"])
        self.invoker = self.new_invoker()
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=AssertionError("Retired sender launched"),
        ):
            self.assertEqual(self.invoker.launch("call-retired", **binding), result)
            self.assertEqual(
                self.invoker.retire_unlaunched("call-retired", binding), result
            )
        self.assertFalse((self.root / "calls").exists())

    def test_claim_without_started_or_result_remains_unknown_and_never_relaunches(self):
        reference = self.input()
        binding = dict(kind="agent", input_ref=reference, deadline=time.time() + 8)
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=OSError("Synthetic lost startup"),
        ):
            first = self.invoker.launch("call-unknown", **binding)
        self.assertEqual(first["status"], "unknown")
        self.invoker = self.new_invoker()
        with patch(
            "research_loop.runtime.invocations.subprocess.Popen",
            side_effect=AssertionError("Unknown sender relaunched"),
        ):
            self.assertEqual(self.invoker.launch("call-unknown", **binding), first)
            self.assertEqual(
                self.invoker.retire_unlaunched("call-unknown", binding), first
            )
        self.assertIsNone(first["proof_ref"])
        self.assertFalse((self.root / "invocations/call-unknown/retired.json").exists())

    def test_binding_and_configuration_changes_are_rejected(self):
        reference = self.input()
        binding = dict(kind="agent", input_ref=reference, deadline=time.time() + 8)
        self.invoker.retire_unlaunched("call-bound", binding)
        with self.assertRaises(ValueError):
            self.invoker.launch("call-bound", **(binding | {"kind": "backend"}))
        changed = LocalProcessInvoker(
            self.root, self.module, self.identity, self.config | {"marker": "changed"}
        )
        with self.assertRaises(ValueError):
            changed.inspect("call-bound")
        with self.assertRaises(ValueError):
            self.invoker.inspect("../escape")

    def test_parent_directories_sync_before_claim_spawn_cancel_and_retirement(self):
        reference = self.input()
        binding = dict(kind="agent", input_ref=reference, deadline=time.time() + 8)
        events = []
        sync = invocations._sync_directory
        publish = invocations.publish_json

        def synchronize(path):
            events.append(("sync", Path(path)))
            sync(path)

        def publication(path, value):
            events.append(("publish", Path(path).name))
            publish(path, value)

        def spawn(*args, **kwargs):
            events.append(("spawn", None))
            raise OSError("Synthetic claimed startup loss")

        with (
            patch.object(invocations, "_sync_directory", synchronize),
            patch.object(invocations, "publish_json", publication),
            patch.object(invocations.subprocess, "Popen", spawn),
        ):
            self.invoker.launch("durable-launch", **binding)
            self.assertLess(
                events.index(("sync", self.root)),
                events.index(("sync", self.root / "invocations")),
            )
            self.assertLess(
                events.index(("sync", self.root / "invocations")),
                events.index(("publish", "claimed.json")),
            )
            self.assertLess(
                events.index(("publish", "claimed.json")), events.index(("spawn", None))
            )
            for action, marker in (
                (
                    lambda: self.invoker.cancel(
                        "durable-cancel", "Synthetic cancellation"
                    ),
                    "cancel.json",
                ),
                (
                    lambda: self.invoker.retire_unlaunched("durable-retire", binding),
                    "retired.json",
                ),
            ):
                events.clear()
                action()
                self.assertLess(
                    events.index(("sync", self.root)),
                    events.index(("sync", self.root / "invocations")),
                )
                self.assertLess(
                    events.index(("sync", self.root / "invocations")),
                    events.index(("publish", marker)),
                )
            self.assertLess(
                events.index(("publish", "retired.json")),
                events.index(("publish", "completion.json")),
            )

    def test_contended_retirement_never_returns_an_unrelated_binding_proof(self):
        binding = dict(
            kind="agent", input_ref=self.input("original"), deadline=time.time() + 8
        )
        original = self.invoker.retire_unlaunched("contended-retire", binding)
        changed = binding | {"input_ref": self.input("different")}
        with _locked(self.root / "invocations/contended-retire") as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(ValueError):
                self.invoker.retire_unlaunched("contended-retire", changed)
            self.assertEqual(
                self.invoker.retire_unlaunched("contended-retire", binding), original
            )
        self.assertEqual(self.invoker.inspect("contended-retire"), original)

    def test_constructor_captures_config_without_aliasing(self):
        reference = self.input(numbers=[2])
        self.config["marker"] = "mutated after construction"
        self.invoker.launch("call-captured", "agent", reference, time.time() + 8)
        self.assertEqual(
            self.completion(self.wait_for("call-captured"))["result"]["marker"],
            "captured",
        )

    def test_changed_source_blocks_new_launch_but_preserves_old_proof(self):
        reference = self.input()
        binding = dict(kind="agent", input_ref=reference, deadline=time.time() + 8)
        completed = self.invoker.retire_unlaunched("original-proof", binding)
        self.module.write_text(MODULE + "\n# changed after source capture\n")
        with self.assertRaisesRegex(ValueError, "module identity changed"):
            self.invoker.launch("new-source", **binding)
        self.assertFalse((self.root / "invocations/new-source/claimed.json").exists())
        self.invoker = self.new_invoker()
        self.assertEqual(self.invoker.inspect("original-proof"), completed)
        self.assertEqual(self.invoker.launch("original-proof", **binding), completed)
        self.assertEqual(self.invoker.cancel("original-proof", "cleanup"), completed)

    def test_worker_rechecks_source_after_parent_launch_check(self):
        reference = self.input()
        spawn = invocations.subprocess.Popen

        def change_then_spawn(*args, **kwargs):
            self.module.write_text(MODULE + "\n# changed before worker import\n")
            return spawn(*args, **kwargs)

        with patch.object(invocations.subprocess, "Popen", change_then_spawn):
            self.invoker.launch(
                "worker-source-fence", "agent", reference, time.time() + 8
            )
        completion = self.completion(self.wait_for("worker-source-fence"))
        self.assertEqual(completion["status"], "failed")
        self.assertIn("module identity changed", completion["error"])
        self.assertFalse((self.root / "calls").exists())


if __name__ == "__main__":
    unittest.main()
