import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop import core
from research_loop.harness import HarnessError
from test_core import ProcessLost, evaluation, read
from test_exploration import ExplorationHarness, POLICY


class ContinuationHarness(ExplorationHarness):
    def response(self, phase, context, kwargs):
        if phase == "execute":
            continuing = "execution_continuation" in context
            self.write_code = not continuing
            self.measure = continuing
            if continuing:
                (Path(kwargs["workspace"]) / "result.json").write_text("{}")
            result = super().response(phase, context, kwargs)
            if not continuing:
                result.update(outcome="incomplete", artifacts=[], measurements=[])
            return result
        return super().response(phase, context, kwargs)


class ExecutionContinuationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"
        initialize(
            self.root,
            goal="Measure one fixed architecture intervention.",
            allow_write=True,
            max_iterations=4,
            max_calls=40,
            strategy={"selection": "direct", "review": "concise"},
            exploration=POLICY,
            global_evaluation=evaluation("quality-v1"),
        )
        inputs = self.root / "research_inputs.json"
        inputs.write_text(json.dumps({"question": "original scientific inputs"}))
        self.harness = ContinuationHarness()
        self.harness.stop_after = 99
        self.study = Study(self.root, self.harness)
        self.state = self.study.run()
        self.source = self.root / "iterations/0001"
        self.target = self.root / "iterations/0002"
        self.record = {
            "source_iteration": "0001",
            "source_workspace_ref": read(self.source / "iteration.json")[
                "workspace_ref"
            ],
            "plan_ref": self.state["plan_ref"],
            "authorization": {"operator": "explicit resume"},
            "reconciliation": {"process": "stopped", "effects": "retained"},
            "directive": "Complete the same measurement from the modified candidate.",
            "source_signatures": {"checkpoint": "adapter metadata"},
        }

    def prepare(self, *, state=None, record=None):
        state = self.study.status() if state is None else state
        with self.study._lock():
            self.study._continue_execution(
                state,
                source_iteration=record["source_iteration"] if record else "0001",
                continuation=self.record if record is None else record,
            )
        return state

    def original_bytes(self):
        return {
            path.relative_to(self.source): path.read_bytes()
            for path in self.source.rglob("*")
            if path.is_file() and "workspace" not in path.relative_to(self.source).parts
        }

    def test_continues_original_plan_and_candidate_without_research_allocation(self):
        original = self.original_bytes()
        before = copy.deepcopy(self.state)
        calls = len(self.harness.calls)
        (self.root / "research_inputs.json").write_text('{"question":"new inputs"}')
        prepared = self.prepare()
        metadata = read(self.target / "iteration.json")
        self.assertEqual(len(self.harness.calls), calls)
        self.assertEqual(prepared["plan_ref"], before["plan_ref"])
        self.assertEqual(prepared["selected"], before["selected"])
        self.assertEqual(prepared["calls"], {})
        for key in (
            "call_count",
            "elapsed_seconds",
            "sampling_queue",
            "sample_counter",
        ):
            self.assertEqual(prepared[key], before[key])
        self.assertEqual(metadata["continuation"], self.record)
        self.assertIsNone(metadata["base_experiment"])
        self.assertEqual(metadata["references"], [])
        self.assertNotIn("input_artifacts", metadata)
        self.assertEqual(metadata["workspace_ref"], self.record["source_workspace_ref"])
        self.assertNotEqual(metadata["workspace_ref"], self.study.config["baseline"])
        self.assertEqual(
            (self.target / "workspace/model.py").read_text(), "version = 1\n"
        )
        self.assertEqual(
            (self.target / "plan.md").read_bytes(),
            (self.source / "plan.md").read_bytes(),
        )
        for name in ("local-evaluations.after.json", "context/route.before.md"):
            self.assertEqual(
                (self.target / name).read_bytes(), (self.source / name).read_bytes()
            )
        self.assertEqual(self.prepare(), prepared)
        self.assertFalse((self.target / "selection.json").exists())
        self.assertFalse((self.target / "plan").exists())
        final = self.study.run()
        new_calls = self.harness.calls[calls:]
        self.assertEqual([call["phase"] for call in new_calls], ["execute", "review"])
        original_plan = read(self.root / before["plan_ref"])
        for call in new_calls:
            context = call["context"]
            self.assertEqual(context["plan"], original_plan)
            self.assertEqual(context["execution_continuation"], self.record)
            self.assertEqual(context["exploration"]["kind"], "architecture")
            self.assertEqual(context["exploration"]["cycle_index"], 0)
            self.assertEqual(
                context["research_inputs"], {"question": "original scientific inputs"}
            )
            self.assertEqual(
                context["global_research"],
                (self.source / "global.after.md").read_text(),
            )
        self.assertEqual(
            new_calls[-1]["context"]["selected_research_design"],
            before["selected"]["proposal"]["research_design"],
        )
        self.assertEqual(self.harness.execute_inputs, [None, "version = 1\n"])
        self.assertEqual(final["call_count"], before["call_count"] + 2)
        self.assertEqual(final["sample_counter"], before["sample_counter"])
        self.assertEqual(self.original_bytes(), original)
        self.assertEqual(
            self.study.frontiers()["global"]["quality-v1"]["candidate_ids"],
            ["0002:quality-v1"],
        )

    def test_preparation_recovers_after_reserved_record_and_partial_files(self):
        original = core._atomic_text
        failed = False

        def crash(path, value):
            nonlocal failed
            if path == self.target / "context/route.before.md" and not failed:
                failed = True
                raise ProcessLost()
            original(path, value)

        calls = len(self.harness.calls)
        with (
            patch.object(core, "_atomic_text", side_effect=crash),
            self.assertRaises(ProcessLost),
        ):
            self.prepare()
        self.assertEqual(self.study.status()["phase"], "selection")
        self.assertEqual(
            read(self.target / "iteration.json")["continuation"], self.record
        )
        state = self.prepare()
        self.assertEqual((state["iteration"], state["phase"]), (2, "execute"))
        self.assertEqual(len(self.harness.calls), calls)
        self.assertEqual(self.prepare(), state)

    def test_context_preserves_original_policy_when_target_snapshot_differs(self):
        self.prepare()
        target_config_path = self.target / "context/study.before.json"
        target_config = read(target_config_path)
        target_config["research_execution"] = {"version": 1}
        target_config["exploration"] = {**POLICY, "cycle": ["diagnostic"]}
        target_config["global_evaluation"] = evaluation("target-evaluation")
        target_config_path.write_text(json.dumps(target_config))
        (self.target / "context/global.before.md").write_text("Current global memory")
        (self.target / "context/routes.before.json").write_text('{"current": {}}')
        (self.target / "context/frontiers.before.json").write_text(
            '{"global": {}, "local": {"current": {}}}'
        )
        (self.target / "context/research_inputs.json").write_text(
            '{"question": "new target inputs"}'
        )
        context = self.study._context(self.target)
        self.assertNotIn("research_execution", context)
        self.assertEqual(context["exploration"]["kind"], "architecture")
        self.assertEqual(context["exploration"]["cycle"], POLICY["cycle"])
        self.assertEqual(context["exploration"]["cycle_index"], 0)
        self.assertEqual(
            context["research_inputs"], {"question": "original scientific inputs"}
        )
        self.assertEqual(context["global_research"], "Current global memory")
        self.assertEqual(context["routes"], {"current": {}})
        self.assertEqual(context["frontiers"]["local"], {"current": {}})
        self.assertEqual(context["global_evaluation"]["id"], "target-evaluation")

    def test_capability_upgrade_resumes_old_plan_without_expanding_its_contract(self):
        path = self.root / "study.json"
        config = read(path)
        config["research_execution"] = {"version": 1}
        path.write_text(json.dumps(config))
        with self.assertRaisesRegex(StudyError, "Reload the current"):
            self.prepare()
        self.assertFalse(self.target.exists())
        self.study = Study(self.root, self.harness)
        self.prepare()
        self.assertEqual(
            read(self.target / "context/study.before.json")["research_execution"],
            {"version": 1},
        )
        calls = len(self.harness.calls)
        final = self.study.run()
        self.assertEqual(final["phase"], "done")
        self.assertEqual(
            [call["phase"] for call in self.harness.calls[calls:]],
            ["execute", "review"],
        )
        for call in self.harness.calls[calls:]:
            self.assertNotIn("research_execution", call["context"])
            self.assertNotIn("research_contract", call["context"]["plan"])
            self.assertEqual(call["context"]["exploration"]["kind"], "architecture")

    def test_scientific_configuration_change_is_refused_after_policy_upgrade(self):
        path = self.root / "study.json"
        config = read(path)
        config["research_execution"] = {"version": 1}
        config["global_evaluation"] = evaluation("changed-protocol")
        path.write_text(json.dumps(config))
        self.study = Study(self.root, self.harness)
        with self.assertRaisesRegex(StudyError, "original study configuration"):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_same_iteration_retry_after_capability_upgrade_reaches_review(self):
        root = self.root.parent / "interrupted-study"
        initialize(
            root,
            goal="Finish the accepted architecture measurement.",
            allow_write=True,
            strategy={"selection": "direct", "review": "concise"},
            exploration=POLICY,
        )
        harness = ExplorationHarness()
        study = Study(root, harness)

        def interrupt(call):
            if call["phase"] == "execute":
                (Path(call["workspace"]) / "model.py").write_text(
                    "partial_candidate = 1\n"
                )
                raise HarnessError("Execution transport interrupted")

        harness.before = interrupt
        with self.assertRaisesRegex(HarnessError, "transport interrupted"):
            study.run()
        interrupted = study.status()
        original_plan = read(root / interrupted["plan_ref"])
        calls = len(harness.calls)
        config = read(root / "study.json")
        config["research_execution"] = {"version": 1}
        (root / "study.json").write_text(json.dumps(config))
        harness.before = None
        study = Study(root, harness)
        study.reconcile_execution(
            recovery_id="verified-transport-interruption",
            certify=lambda state: {
                "source_iteration": "0001",
                "source_attempt": interrupted["calls"]["execute"],
                "plan_ref": interrupted["plan_ref"],
                "action": "retry",
                "effect": "never_submitted",
                "summary": "Trusted fixture transport confirms no external execution was submitted.",
                "proof_refs": ["backend/unsubmitted-transport.json"],
            },
        )
        final = study.run()
        self.assertEqual((final["iteration"], final["phase"]), (1, "done"))
        self.assertEqual(final["plan_ref"], interrupted["plan_ref"])
        self.assertEqual(final["sample_counter"], interrupted["sample_counter"])
        self.assertEqual(
            [call["phase"] for call in harness.calls[calls:]], ["execute", "review"]
        )
        for call in harness.calls[calls:]:
            self.assertEqual(call["context"]["plan"], original_plan)
            self.assertNotIn("research_execution", call["context"])
            self.assertEqual(call["context"]["exploration"]["kind"], "architecture")
        self.assertEqual(harness.execute_inputs, ["partial_candidate = 1\n"])
        self.assertTrue((root / "iterations/0001/publication.json").exists())

    def test_review_boundary_still_refuses_scientific_configuration_changes(self):
        original_config = read(self.root / "study.json")
        before = (self.source / "review.before.json").read_bytes()
        for field, value in (
            ("global_evaluation", evaluation("changed-protocol")),
            ("max_calls", original_config["max_calls"] + 1),
        ):
            with self.subTest(field=field):
                config = {
                    **original_config,
                    "research_execution": {"version": 1},
                    field: value,
                }
                (self.root / "study.json").write_text(json.dumps(config))
                with self.assertRaisesRegex(StudyError, "configuration changed"):
                    self.study._review_before(self.source, self.state["route_id"])
                self.assertEqual(
                    (self.source / "review.before.json").read_bytes(), before
                )

    def test_preparation_recovers_after_begin_before_continuation_metadata(self):
        state = self.study.status()
        with self.study._lock():
            self.study._begin(state)
        self.assertEqual(self.prepare()["phase"], "execute")

    def test_completed_or_measured_source_is_not_continuable(self):
        path = self.root / self.state["execution_ref"]
        response = read(path)
        response["outcome"] = "completed"
        path.write_text(json.dumps(response))
        with self.assertRaisesRegex(StudyError, "unmeasured incomplete"):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_changed_contract_and_version_are_rejected_before_reserving_record(self):
        bad = {**self.record, "source_workspace_ref": "foreign-version"}
        with self.assertRaisesRegex(StudyError, "source workspace version"):
            self.prepare(record=bad)
        (self.root / "goal.md").write_text("Changed scientific question")
        with self.assertRaisesRegex(StudyError, "frozen goal"):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_replay_rejects_changed_pointer_and_preserves_foreign_workspace(self):
        state = self.prepare()
        workspace = self.target / "workspace"
        core._git(workspace, "reset", "--hard", self.study.config["baseline"])
        with self.assertRaisesRegex(StudyError, "workspace version"):
            self.prepare()
        self.assertEqual(
            core._git(workspace, "rev-parse", "HEAD"), self.study.config["baseline"]
        )
        core._git(workspace, "reset", "--hard", self.record["source_workspace_ref"])
        state["plan_ref"] = "iterations/9999/foreign.json"
        with self.assertRaisesRegex(StudyError, "state does not match"):
            self.prepare(state=state)

    def test_original_plan_can_precede_legacy_physical_source(self):
        # Model a previously published operator execution whose scientific Plan
        # remains the first iteration's immutable accepted Plan.
        self.prepare()
        second_record = read(self.target / "iteration.json")["continuation"]
        self.assertEqual(second_record, self.record)
        original_response = self.harness.response

        def incomplete(phase, context, kwargs):
            result = original_response(phase, context, kwargs)
            if phase == "execute":
                result.update(outcome="incomplete", artifacts=[], measurements=[])
            return result

        self.harness.response = incomplete
        state = self.study.run()
        publication_path = self.target / "publication.json"
        publication = read(publication_path)
        publication["state_after"]["plan_ref"] = "iterations/0002/operator-plan.json"
        publication_path.write_text(json.dumps(publication))
        record = {
            **self.record,
            "source_iteration": "0002",
            "source_workspace_ref": read(self.target / "iteration.json")[
                "workspace_ref"
            ],
        }
        with self.study._lock():
            self.study._continue_execution(
                state, source_iteration="0002", continuation=record
            )
        third = self.root / "iterations/0003"
        self.assertEqual(state["plan_ref"], self.record["plan_ref"])
        self.assertEqual(read(third / "iteration.json")["base_experiment"], None)
        self.assertEqual(self.study._context(third)["exploration"]["cycle_index"], 0)
        self.assertEqual(
            (third / "plan.md").read_bytes(), (self.source / "plan.md").read_bytes()
        )


if __name__ == "__main__":
    unittest.main()
