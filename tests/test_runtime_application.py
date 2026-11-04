"""Explicit trusted applications, without any model or external backend."""

import json
from pathlib import Path
import tempfile
import time
import unittest

from research_loop.runtime.application import (
    ROLES,
    load_application,
    prepare_application,
)
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.invocations import LocalProcessInvoker
from research_loop.runtime.store import StateStore


ADAPTER = {"id": "application-fixture", "version": "1"}
MODULE = """
from pathlib import Path

APPLICATION_IDENTITY = {"id": "application-fixture", "version": "1"}

def invoke(kind, payload, config):
    with Path(config['events']).open('a') as stream:
        stream.write('invoke\\n')
    return {'kind': kind, 'payload': payload, 'marker': config['nested']['marker']}

def validate(role, output, context, config):
    with Path(config['events']).open('a') as stream:
        stream.write('validate\\n')
    marker = config['nested']['marker']
    config['nested']['marker'] = 'validator-local mutation'
    if output.get('invalid'):
        raise ValueError('Synthetic role rejection')
    return {'role': role, 'output': output, 'context': context, 'marker': marker}
"""


class RuntimeApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.module = self.root / "application.py"
        self.module.write_text(MODULE)
        self.events = self.root / "events"
        self.config = {
            "events": str(self.events),
            "nested": {"marker": "frozen"},
        }

    def prepare(self):
        return prepare_application(self.module, self.config, ADAPTER)

    def load(self, spec):
        store = StateStore.create(
            self.root / "study",
            config={"runtime": {"worker_adapter": ADAPTER, "application": spec}},
        )
        session = store.coordinator()
        owner = session.__enter__()
        self.addCleanup(lambda: session.__exit__(None, None, None))
        return owner, load_application(owner)

    def wait(self, invoker, token):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            observation = invoker.inspect(token)
            if observation["completion_ref"] is not None:
                return observation
            time.sleep(0.02)
        self.fail("Synthetic application did not finish")

    def test_prepare_and_load_do_not_invoke_callbacks_and_detach_config(self):
        spec = self.prepare()
        self.assertEqual(set(spec), {"module", "module_identity", "config"})
        self.assertEqual(spec["module"], str(self.module.resolve()))
        self.assertEqual(
            set(spec["module_identity"]), {"device", "inode", "size", "mtime_ns"}
        )
        self.assertEqual(json.loads(json.dumps(spec)), spec)
        self.config["nested"]["marker"] = "caller mutation"
        owner, app = self.load(spec)
        spec["config"]["nested"]["marker"] = "spec mutation"
        self.assertFalse(self.events.exists())
        self.assertEqual(set(app.validators), set(ROLES))
        for role in ROLES:
            normalized = app.validators[role]({"value": 1}, {"task": None})
            self.assertEqual(normalized["role"], role)
            self.assertEqual(normalized["marker"], "frozen")
            self.assertEqual(normalized["output"], {"value": 1})
            self.assertEqual(normalized["context"], {"task": None})
        with self.assertRaisesRegex(ValueError, "Synthetic role rejection"):
            app.validators["plan"]({"invalid": True}, {})
        self.assertEqual(owner.snapshot()["counters"]["calls"], 0)
        reference = publish_artifact(owner.store.root, {"value": 2})
        app.invoker.launch("application-call", "agent", reference, time.time() + 8)
        observation = self.wait(app.invoker, "application-call")
        result = json.loads(
            (owner.store.root / observation["completion_ref"]).read_text()
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["marker"], "frozen")
        self.assertEqual(result["result"]["payload"], {"value": 2})

    def test_declared_identity_and_required_callbacks_are_checked(self):
        with self.assertRaisesRegex(ValueError, "differs"):
            prepare_application(self.module, {}, ADAPTER | {"version": "2"})
        self.module.write_text(MODULE.replace("def validate(", "def not_validate("))
        with self.assertRaisesRegex(ValueError, "must define validate"):
            self.prepare()
        self.module.write_text(MODULE.replace("def invoke(", "def not_invoke("))
        with self.assertRaisesRegex(ValueError, "must define invoke"):
            self.prepare()
        self.assertFalse(self.events.exists())

    def test_non_json_config_and_malformed_spec_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_application(self.module, {"value": float("nan")}, ADAPTER)
        spec = self.prepare()
        for invalid in (
            spec | {"extra": True},
            spec | {"module": "relative.py"},
            spec | {"config": []},
            spec | {"module_identity": {"size": 0}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.load(invalid)
            # Each invalid load gets an independent actual SQLite store.
            self.root = self.root / "next"
            self.root.mkdir()

    def test_source_change_fences_load_launch_and_validation_without_callback(self):
        spec = self.prepare()
        owner, app = self.load(spec)
        self.module.write_text(MODULE + "\n# changed source\n")
        with self.assertRaisesRegex(ValueError, "module identity changed"):
            load_application(owner)
        with self.assertRaisesRegex(ValueError, "module identity changed"):
            app.validators["plan"]({}, {})
        reference = publish_artifact(owner.store.root, {})
        with self.assertRaisesRegex(ValueError, "module identity changed"):
            app.invoker.launch("changed-source", "agent", reference, time.time() + 8)
        self.assertFalse(self.events.exists())
        self.assertFalse(
            (owner.store.root / "invocations/changed-source/claimed.json").exists()
        )

    def test_old_completion_survives_changed_or_missing_module(self):
        spec = self.prepare()
        owner, app = self.load(spec)
        reference = publish_artifact(owner.store.root, {})
        binding = {"kind": "agent", "input_ref": reference, "deadline": time.time() + 8}
        app.invoker.launch("completed-original", **binding)
        completion = self.wait(app.invoker, "completed-original")
        self.module.unlink()
        recovery = LocalProcessInvoker(
            owner.store.root,
            spec["module"],
            ADAPTER,
            spec["config"],
            module_identity=spec["module_identity"],
        )
        self.assertEqual(recovery.inspect("completed-original"), completion)
        self.assertEqual(recovery.cancel("completed-original", "cleanup"), completion)
        self.assertEqual(recovery.launch("completed-original", **binding), completion)
        self.assertEqual(
            recovery.retire_unlaunched("completed-original", binding), completion
        )
        retired = recovery.retire_unlaunched("never-started", binding)
        self.assertEqual(retired["status"], "not_started")

    def test_source_changed_during_import_is_rejected(self):
        self.module.write_text(
            MODULE + "\nPath(__file__).write_text('# synthetic self modification')\n"
        )
        with self.assertRaisesRegex(ValueError, "module identity changed"):
            self.prepare()
        self.assertFalse(self.events.exists())


if __name__ == "__main__":
    unittest.main()
