"""Synthetic authority-transfer crashes; never invoke a model or backend."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ruamel.yaml import YAML

from examples import coordinator_fixture
from examples.concurrent_runtime import configuration
from research_loop.cli import main
from research_loop.core import Study, initialize
from research_loop.runtime.application import prepare_application
from research_loop.runtime.format import RuntimeFormatError
from research_loop.runtime.legacy_inventory import capture_legacy
from research_loop.runtime.migration import (
    activate_import,
    bind_imported_execute,
    commit_import,
    prepare_import,
    resolve_import,
)
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    StateStore,
    SCHEMA_VERSION,
)
from research_loop.tasks import TaskQueue


class RuntimeMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "study"
        initialize(
            self.root,
            goal="Preserve the exact scientific question",
            max_calls=17,
            max_iterations=5,
            max_seconds=3600,
        )
        self.application = self.base / "application.py"
        self.application.write_text(Path(coordinator_fixture.__file__).read_text())

    def arguments(self):
        inventory = capture_legacy(self.root)
        config = configuration()
        config.update(
            execution_backends={}, resource_pools={}, operation_authorizations={}
        )
        config.update(
            goal=inventory["records"]["goal.md"]["text"],
            permissions={"allow_write": False},
            global_evaluation=None,
        )
        config["runtime"]["application"] = prepare_application(
            self.application,
            {"root": str(self.root)},
            config["runtime"]["worker_adapter"],
        )
        translation = {
            "version": 1,
            "translator": {"id": "synthetic-legacy", "version": "1"},
            "source_refs": ["study.json", "state.json"],
            "executions": [],
            "authorities": [],
            "blockers": [],
        }
        fence = {
            "version": 1,
            "source_identity": inventory["source_identity"],
            "launchers_disabled": True,
            "senders_retired": True,
            "evidence_refs": ["synthetic:no-processes-ever-started"],
        }
        deployment = {
            "version": 1,
            "runtime_schema": SCHEMA_VERSION,
            "revision": "a" * 40,
            "application": config["runtime"]["worker_adapter"],
            "environment": {"id": "synthetic-local", "version": "1"},
        }
        return inventory, translation, config, fence, deployment

    def prepare(self, arguments=None):
        return prepare_import(
            self.root, *(arguments or self.arguments()), import_id="one"
        )

    def evidence(self):
        return {
            "import_id": "one",
            "expected_revision": 1,
            "authorization_evidence_refs": ["synthetic:unchanged-authorization"],
            "time_evidence_refs": ["synthetic:known-gap"],
            "reconciliation_evidence_refs": [],
            "additional_elapsed_seconds": 3.5,
        }

    def test_cli_prepare_accepts_yaml_config_and_json_protocol_documents(self):
        inventory, translation, config, fence, deployment = self.arguments()
        del config["runtime"]["application"]
        config_path = self.base / "config.yml"
        YAML(typ="safe", pure=True).dump(config, config_path)
        app_path = self.base / "application.yaml"
        YAML(typ="safe", pure=True).dump({"root": str(self.root)}, app_path)
        arguments = [
            "runtime",
            "migrate",
            "prepare",
            str(self.root),
            "--import-id",
            "cli",
            "--config",
            str(config_path),
            "--application",
            str(self.application),
            "--application-config",
            str(app_path),
        ]
        for name, value in (
            ("inventory", inventory),
            ("translation", translation),
            ("fence", fence),
            ("deployment", deployment),
        ):
            path = self.base / (name + ".json")
            path.write_text(json.dumps(value))
            arguments.extend(["--" + name, str(path)])
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(arguments), 0, errors.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["database"], ".runtime-imports/cli/runtime.sqlite3")
        self.assertTrue((self.root / result["database"]).is_file())
        self.assertEqual(result["import_id"], "cli")
        self.assertFalse((self.root / "runtime.sqlite3").exists())

    def test_preserve_spending_stop_pending_task_and_exact_source(self):
        state_path = self.root / "state.json"
        state = json.loads(state_path.read_text())
        state.update(
            call_count=19,
            iteration=6,
            elapsed_seconds=80.25,
            status="stopped",
            stop_reason="Original STOP",
        )
        state_path.write_text(json.dumps(state, indent=3) + "\n")
        folder = self.root / "tasks/human"
        folder.mkdir(parents=True)
        (folder / "request.json").write_text(
            json.dumps(
                {
                    "id": "human",
                    "kind": "investigation",
                    "title": "Frozen request",
                    "instruction": "Exact instruction",
                    "allow_changes": False,
                    "priority": 100,
                    "source": {"revision": "synthetic-version"},
                }
            )
        )
        (folder / "scheduling.json").write_text(json.dumps({"priority": 7}))
        original = state_path.read_bytes()
        arguments = self.arguments()
        prepared = self.prepare(arguments)
        self.assertEqual(self.prepare(arguments), prepared)
        self.assertEqual(Study(self.root).status(), state)
        committed = commit_import(self.root, "one")
        self.assertEqual(committed["submissions"], 0)
        self.assertEqual(state_path.read_bytes(), original)
        store = StateStore(self.root)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["limits"], arguments[0]["limits"])
        self.assertEqual(snapshot["counters"]["calls"], 19)
        self.assertEqual(snapshot["counters"]["iterations"], 6)
        self.assertGreaterEqual(snapshot["counters"]["elapsed_seconds"], 80.25)
        self.assertEqual(snapshot["stop"]["reason"], "Original STOP")
        self.assertEqual(snapshot["admission_mode"], "inspect_only")
        self.assertEqual(snapshot["tasks"][0]["source"], "human")
        self.assertEqual(snapshot["tasks"][0]["priority"], 7)
        self.assertFalse(
            snapshot["tasks"][0]["specification"]["legacy_request"]["allow_changes"]
        )
        self.assertEqual(snapshot["knowledge_version"], 0)
        self.assertTrue(commit_import(self.root, "one")["replayed"])
        with self.assertRaises(RuntimeFormatError):
            Study(self.root)
        with self.assertRaises(RuntimeFormatError):
            TaskQueue(self.root)

    def test_every_cutover_boundary_is_fail_closed_and_same_import_resumes(self):
        for boundary in ("preparing", "database", "committed"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as temporary,
            ):
                original_root = self.root
                self.root = Path(temporary).resolve() / "study"
                initialize(self.root, goal="Preserve the exact scientific question")
                try:
                    self.prepare()

                    def crash(step):
                        if step == boundary:
                            raise RuntimeError("injected interruption")

                    with self.assertRaisesRegex(RuntimeError, "injected"):
                        commit_import(self.root, "one", _after_step=crash)
                    with self.assertRaises(RuntimeFormatError):
                        Study(self.root)
                    if boundary != "committed":
                        with self.assertRaises(RuntimeFormatError):
                            StateStore(self.root)
                    commit_import(self.root, "one")
                    self.assertEqual(
                        StateStore(self.root).snapshot()["admission_mode"],
                        "inspect_only",
                    )
                    self.assertTrue(commit_import(self.root, "one")["replayed"])
                finally:
                    self.root = original_root

    def test_changed_source_and_unfenced_sender_prevent_switch(self):
        arguments = list(self.arguments())
        arguments[3]["senders_retired"] = False
        with self.assertRaisesRegex(ValueError, "fence"):
            self.prepare(arguments)
        self.assertFalse((self.root / ".runtime-imports").exists())
        self.prepare()
        (self.root / "goal.md").write_text("Changed question\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            commit_import(self.root, "one")
        self.assertFalse((self.root / "runtime.json").exists())

    def test_invalid_original_counter_cannot_become_admission_credit(self):
        state_path = self.root / "state.json"
        state = json.loads(state_path.read_text())
        state["call_count"] = -3
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "original calls"):
            self.prepare()
        self.assertFalse((self.root / ".runtime-imports").exists())

    def test_interrupted_staging_rebuilds_only_its_unpublished_database(self):
        arguments = self.arguments()
        with patch(
            "research_loop.runtime.migration._checkpoint",
            side_effect=RuntimeError("crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash"):
                self.prepare(arguments)
        self.assertFalse((self.root / "runtime.json").exists())
        self.prepare(arguments)
        commit_import(self.root, "one")
        self.assertEqual(StateStore(self.root).snapshot()["counters"]["calls"], 0)

    def test_activation_charges_once_and_preserves_stop(self):
        self.prepare()
        commit_import(self.root, "one")
        store = StateStore(self.root)
        with store.coordinator() as owner:
            owner.create_task("new", {"instruction": "Future investigation"})
            with self.assertRaises(BudgetExceeded):
                owner.claim_task("new")
            owner.set_stop("Original STOP")
            first = activate_import(owner, "one", self.evidence())
            before = owner.snapshot()["counters"]["elapsed_seconds"]
            second = activate_import(owner, "one", self.evidence())
            after = owner.snapshot()["counters"]["elapsed_seconds"]
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertLess(after - before, 1)
            self.assertEqual(owner.snapshot()["stop"]["reason"], "Original STOP")
            with self.assertRaises(BudgetExceeded):
                owner.claim_task("new")
            with self.assertRaises(ConflictError):
                activate_import(
                    owner, "one", {**self.evidence(), "additional_elapsed_seconds": 4}
                )

    def test_control_stop_cancelled_id_and_orphan_sidecar_are_preserved(self):
        (self.root / "control").mkdir()
        (self.root / "control/STOP").write_text("Keep stopped")
        task = self.root / "tasks/cancelled"
        task.mkdir(parents=True)
        (task / "request.json").write_text(
            json.dumps({"id": "cancelled", "kind": "investigation", "priority": 10})
        )
        (task / "cancelled.json").write_text("{}")
        self.prepare()
        sidecar = self.root / "runtime.sqlite3-wal"
        sidecar.write_bytes(b"unrelated interrupted database")
        with self.assertRaisesRegex(ConflictError, "sidecars"):
            commit_import(self.root, "one")
        self.assertFalse((self.root / "runtime.json").exists())
        sidecar.unlink()
        commit_import(self.root, "one")
        store = StateStore(self.root)
        self.assertEqual(store.snapshot()["stop"]["reason"], "Keep stopped")
        with store.coordinator() as owner:
            with self.assertRaises(ConflictError):
                owner.create_task("cancelled", {})

    def test_resolution_is_specific_revision_bound_and_does_not_change_accounting(self):
        arguments = list(self.arguments())
        arguments[1]["blockers"] = ["needs-original-evidence"]
        self.prepare(arguments)
        commit_import(self.root, "one")
        evidence = {
            "import_id": "one",
            "expected_revision": 1,
            "resolutions": [
                {
                    "blocker": "needs-original-evidence",
                    "reason": "Synthetic original receipt checked",
                    "evidence_refs": ["synthetic:original-receipt"],
                }
            ],
        }
        with StateStore(self.root).coordinator() as owner:
            with self.assertRaisesRegex(Exception, "blockers"):
                activate_import(owner, "one", self.evidence())
            before = owner.snapshot()
            receipt = resolve_import(owner, "one", evidence)
            self.assertEqual(receipt["remaining_blockers"], [])
            self.assertTrue(resolve_import(owner, "one", evidence)["replayed"])
            after = owner.snapshot()
            self.assertEqual(after["counters"]["calls"], before["counters"]["calls"])
            self.assertEqual(after["stop"], before["stop"])
            self.assertEqual(after["admission_mode"], "inspect_only")
            self.assertFalse(
                activate_import(
                    owner, "one", {**self.evidence(), "expected_revision": 2}
                )["replayed"]
            )
            with self.assertRaises(ConflictError):
                resolve_import(
                    owner,
                    "one",
                    {
                        **evidence,
                        "resolutions": [
                            {**evidence["resolutions"][0], "reason": "Changed"}
                        ],
                    },
                )

    def test_original_execute_settlement_does_not_create_calls_or_release_operations(
        self,
    ):
        from copy import deepcopy
        from examples.backend_lifecycle import CONFIG
        from tests.test_runtime_migration_records import translation
        from research_loop.runtime.operations import Operations
        from research_loop.runtime.store import InvalidTransition

        state_path = self.root / "state.json"
        state = json.loads(state_path.read_text())
        state.update(call_count=2, iteration=1)
        state_path.write_text(json.dumps(state))
        arguments = list(self.arguments())
        arguments[1] = translation()
        for key in ("execution_backends", "resource_pools", "operation_authorizations"):
            arguments[2][key] = deepcopy(CONFIG[key])
        self.prepare(arguments)
        commit_import(self.root, "one")
        evidence = {
            "import_id": "one",
            "attempt_id": "old-execute",
            "expected_revision": 1,
            "normalized_output": {"artifacts": []},
            "evidence_refs": ["synthetic:original-effect-settlement"],
        }
        with StateStore(self.root).coordinator() as owner:
            before = owner.snapshot()["counters"]
            result = bind_imported_execute(owner, "one", evidence)
            self.assertFalse(result["replayed"])
            self.assertTrue(bind_imported_execute(owner, "one", evidence)["replayed"])
            after = owner.snapshot()
            self.assertEqual(after["counters"]["calls"], before["calls"])
            self.assertEqual(after["counters"]["iterations"], before["iterations"])
            self.assertEqual(after["iterations"][0]["status"], "active")
            operation = Operations(owner).get("old-operation")
            self.assertFalse(operation["physical_released"])
            self.assertTrue(operation["submit_forbidden"])
            with owner.store._connection(readonly=True) as conn:
                with self.assertRaises(InvalidTransition):
                    Operations.check_review_ready(conn, "old-iteration")
            with self.assertRaises(ConflictError):
                bind_imported_execute(
                    owner,
                    "one",
                    {
                        **evidence,
                        "normalized_output": {"artifacts": [], "changed": True},
                    },
                )
