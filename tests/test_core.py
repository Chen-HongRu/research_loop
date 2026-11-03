import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_loop import Study, StudyError, initialize
from research_loop.exploration import stage_schema
from research_loop.harness import HarnessError, publish_completed_response
from research_loop.long_training import stage_schema as long_training_schema
from research_loop.protocol import SCHEMAS


def read(path):
    return json.loads(path.read_text())


def evaluation(name):
    return {
        "id": name,
        "comparison_scope": "fixed-split-v1",
        "protocol": "One fixed checkpoint and deterministic evaluation.",
        "data_role": "development",
        "allow_feedback": True,
        "objectives": [
            {"name": "quality", "direction": "maximize", "unit": "fraction"},
            {"name": "cost", "direction": "minimize", "unit": "seconds"},
        ],
        "constraints": [],
    }


class ProcessLost(BaseException):
    """A controller death after or before durable completion."""


class RecordedHarness:
    """Exercise real envelopes, contexts, receipts and filesystem publication."""

    def __init__(self):
        self.calls = []
        self.stop_after = 1
        self.judge_decision = "select"
        self.base = None
        self.local = None
        self.write_code = False
        self.write_helper = False
        self.choose_new = False
        self.measure = False
        self.quality = 0.9
        self.corrections = []
        self.before = None
        self.after = None
        self.review_count = 0
        self.execute_inputs = []
        self.helper_inputs = []

    def run(self, **kwargs):
        phase = re.search(r"^Stage: (\w+)$", kwargs["prompt"], re.MULTILINE)[1]
        context = read(kwargs["run_dir"] / "context.json")
        call = {**kwargs, "phase": phase, "context": context}
        self.calls.append(call)
        if self.before:
            self.before(call)
        result = self.response(phase, context, kwargs)
        properties = kwargs["schema"]["properties"]
        if phase == "execute" and set(properties) <= {"outcome", "markdown"}:
            result = {key: result[key] for key in properties}
        (kwargs["run_dir"] / "prompt.md").write_text(kwargs["prompt"])
        (kwargs["run_dir"] / "response.json").write_text(json.dumps(result))
        (kwargs["run_dir"] / "process.json").write_text(
            json.dumps({"status": "succeeded"})
        )
        if self.after:
            self.after(call)
        return result

    def response(self, phase, context, kwargs):
        if phase == "proposal":
            return {
                "markdown": "Compare a bounded intervention; mechanism remains unknown.",
                **({"route_id": "new"} if context["route"]["id"] == "new" else {}),
                "route_title": "A useful empirical question",
                "action": "Measure one bounded intervention",
                "base_experiment": self.base,
                "references": [self.base] if self.base else [],
            }
        if phase in ("critique", "response"):
            return {
                "markdown": "Evidence has limited scope; no causal claim is established."
            }
        if phase == "judge":
            side = next(
                (
                    s
                    for s, p in context["proposals"].items()
                    if (p["route_id"] == "new") == self.choose_new
                ),
                "a",
            )
            return {
                "decision": self.judge_decision,
                "choice": side if self.judge_decision == "select" else None,
                "action": context["proposals"][side]["action"],
                "markdown": "Selected for useful evidence under the available budget.",
                "goal_status": "undetermined",
                "stop_reason": "No feasible investigation"
                if self.judge_decision == "stop"
                else None,
            }
        if phase == "plan":
            return {
                "decision": "investigate",
                "markdown": (
                    "Resolve a useful empirical uncertainty in one bounded investigation. "
                    f"Action: {context['selected']['judge']['action']}. "
                    "Use the frozen evaluation protocol and selected starting version; "
                    "no additional input artifacts are required. Retain actual output "
                    "and record missing results explicitly. Interpret practical observations "
                    "without claiming an established mechanism."
                ),
                "route_research": "Question and evidence gap; no mechanism claim.",
                **(
                    {
                        "base_experiment": context["selected"]["proposal"][
                            "base_experiment"
                        ]
                    }
                    if "base_experiment" in kwargs["schema"]["properties"]
                    else {}
                ),
                "references": context["selected"]["proposal"]["references"],
                "local_evaluation": self.local,
                "timeout_seconds": 60,
                "stop_reason": None,
            }
        if phase == "execute":
            work = Path(kwargs["workspace"])
            code = work / "model.py"
            self.execute_inputs.append(code.read_text() if code.exists() else None)
            helper = work / "helper.py"
            self.helper_inputs.append(helper.read_text() if helper.exists() else None)
            artifacts = []
            if self.write_code:
                code.write_text(f"version = {len(self.execute_inputs)}\n")
                artifacts.append(
                    {"path": "model.py", "version": f"v{len(self.execute_inputs)}"}
                )
            if self.write_helper:
                helper.write_text("def helper():\n    return 1\n")
            measurements = []
            if self.measure:
                artifact = work / "result.json"
                if not artifact.is_file():
                    raise AssertionError("The evaluation fixture must already exist")
                artifacts.append({"path": "result.json", "version": "v1"})
                definitions = [
                    context["global_evaluation"],
                    context["plan"]["local_evaluation"],
                ]
                for definition in filter(None, definitions):
                    measurements.append(
                        {
                            "candidate_id": definition["id"],
                            "artifact": {"path": "result.json", "version": "v1"},
                            "evaluation_id": definition["id"],
                            "comparison_scope": definition["comparison_scope"],
                            "metrics": [
                                {"name": "quality", "value": self.quality},
                                {"name": "cost", "value": 2},
                            ],
                            "evidence": [str(artifact)],
                        }
                    )
            return {
                "outcome": "completed",
                "markdown": "Practical result observed; mechanism unknown.",
                "artifacts": artifacts,
                "measurements": measurements,
            }
        self.review_count += 1
        return {
            "markdown": "Observed practical evidence; mechanism remains unknown.",
            "route_research": f"Route observations after review {self.review_count}; evidence and limitations retained.",
            "global_research": f"Global observations after review {self.review_count}; unresolved mechanism.",
            "route_status": "active",
            "route_reason": "The empirical question remains useful.",
            "resume_condition": None,
            "goal_status": "undetermined",
            "decision": "stop" if self.review_count >= self.stop_after else "continue",
            "candidate_reviews": [
                {
                    "candidate_id": m["candidate_id"],
                    "valid": True,
                    "verified": True,
                    "reason": "Checked actual fixed-version output.",
                }
                for m in context["execute"]["measurements"]
            ],
            "candidate_corrections": self.corrections,
            "knowledge_changes": ["Practical evidence recorded; no causal inference."],
        }

    def count(self, phase):
        return sum(call["phase"] == phase for call in self.calls)


class CoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "study"

    def study(self, **options):
        initialize(
            self.root,
            goal="Improve practical performance; causal explanation is optional.",
            **options,
        )
        harness = RecordedHarness()
        study = Study(self.root, harness)
        if options.get("global_evaluation"):
            (study.workspace / "result.json").write_text("{}")
        return study, harness

    def test_older_study_format_rejected_before_mutation(self):
        study, harness = self.study()
        config_path = self.root / "study.json"
        self.assertEqual(read(config_path)["version"], 3)
        retained = self.root / "retained-evidence.txt"
        retained.write_text("Historical evidence must remain unchanged.")
        for version in (1, 2):
            with self.subTest(version=version):
                config = read(config_path)
                config["version"] = version
                config_path.write_text(json.dumps(config))
                before = {
                    path.relative_to(self.root): path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                with self.assertRaisesRegex(StudyError, "Unsupported study format"):
                    Study(self.root, harness)
                self.assertEqual(
                    {
                        path.relative_to(self.root): path.read_bytes()
                        for path in self.root.rglob("*")
                        if path.is_file()
                    },
                    before,
                )
                self.assertEqual(harness.calls, [])

    def test_readonly_feedback_retains_rejected_outputs_and_never_repeats_execute(self):
        for failed_phase in ("plan", "review"):
            with self.subTest(phase=failed_phase):
                self.root = self.root.parent / failed_phase
                study, harness = self.study()
                original = harness.response
                rejected = False

                def response(phase, context, kwargs):
                    nonlocal rejected
                    value = original(phase, context, kwargs)
                    if phase == failed_phase and not rejected:
                        rejected = True
                        if phase == "plan":
                            value["references"] = ["invalid-reference"]
                        else:
                            value["decision"] = "invalid-decision"
                    return value

                harness.response = response
                state = study.run()
                calls = [
                    call for call in harness.calls if call["phase"] == failed_phase
                ]
                attempt = calls[0]["run_dir"]
                raw = (attempt / "response.json").read_bytes()
                self.assertEqual(
                    read(attempt / "rejection.json")["response"], json.loads(raw)
                )
                self.assertEqual(calls[0]["context"], calls[1]["context"])
                self.assertIn("previous reply was rejected", calls[1]["prompt"])
                self.assertEqual((attempt / "response.json").read_bytes(), raw)
                self.assertEqual(state["call_count"], len(harness.calls))
                self.assertEqual(harness.count(failed_phase), 2)
                self.assertEqual(harness.count("execute"), 1)
                self.assertTrue(
                    (self.root / "iterations/0001/publication.json").exists()
                )

    def test_plan_rejects_model_supplied_base_before_execute(self):
        study, harness = self.study(max_calls=12)
        original = harness.response

        def injected(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "plan":
                result["base_experiment"] = None
            return result

        harness.response = injected
        self.assertEqual(study.run()["stop_reason"], "call_budget")
        self.assertEqual(harness.count("execute"), 0)
        call = next(c for c in harness.calls if c["phase"] == "plan")
        self.assertIn("base_experiment", read(call["run_dir"] / "response.json"))
        self.assertIn(
            "base_experiment",
            json.dumps(read(call["run_dir"] / "rejection.json")["errors"]),
        )

    def legacy_completed_call(self, phase, *, accepted=False):
        study, harness = self.study()
        harness.stop_after = 2
        study.run()
        harness.base = "0001"
        response = harness.response

        def legacy_response(phase, context, kwargs):
            result = response(phase, context, kwargs)
            if phase == "proposal":
                result["route_id"] = context["route"]["id"]
            return result

        def target(call):
            return call["phase"] == phase and (
                phase != "proposal" or call["context"]["route"]["id"] != "new"
            )

        def completed(call):
            if target(call):
                publish_completed_response(
                    call["run_dir"], read(call["run_dir"] / "response.json")
                )
                if not accepted:
                    raise ProcessLost()

        original_call = study._call

        def call(*args, **kwargs):
            result = original_call(*args, **kwargs)
            if accepted and target({"phase": args[1], "context": args[3]}):
                raise ProcessLost()
            return result

        def legacy_schema(phase, context):
            return long_training_schema(
                phase, context, stage_schema(phase, context, SCHEMAS)
            )

        harness.response = legacy_response
        harness.after = completed
        with (
            patch.object(study, "_agent_schema", side_effect=legacy_schema),
            patch.object(study, "_call", side_effect=call),
            self.assertRaises(ProcessLost),
        ):
            study.run()
        harness.after = None
        state = study.status()
        key = state["last_call_key"]
        attempt = self.root / state["calls"][key]
        return study, harness, state, key, attempt

    def test_legacy_completed_plan_and_proposal_reuse_original_receipts(self):
        for phase in ("plan", "proposal"):
            for accepted in (False, True):
                with self.subTest(phase=phase, accepted=accepted):
                    self.root = self.root.parent / f"{phase}-{accepted}"
                    study, harness, state, key, attempt = self.legacy_completed_call(
                        phase, accepted=accepted
                    )
                    originals = {
                        p: (attempt / p).read_bytes()
                        for p in (
                            "response.json",
                            "completed.json",
                            "schema.json",
                            "context.json",
                        )
                    }
                    calls = len(harness.calls)
                    count = state["call_count"]
                    resumed = Study(self.root, harness)
                    if phase == "plan":
                        resumed._step(state)
                        self.assertEqual(state["phase"], "execute")
                        self.assertEqual(
                            read(self.root / state["plan_ref"])["base_experiment"],
                            "0001",
                        )
                    else:
                        context = read(attempt / "context.json")
                        context["budget"] = {"calls_left": 0, "seconds_left": 0}
                        result = resumed._call(
                            state, phase, key, context, attempt.parent
                        )
                        self.assertEqual(result["route_id"], context["route"]["id"])
                    self.assertEqual(len(harness.calls), calls)
                    self.assertEqual(state["call_count"], count)
                    self.assertTrue(state["accepted_calls"][key])
                    self.assertEqual(harness.count("execute"), 1)
                    for path, raw in originals.items():
                        self.assertEqual((attempt / path).read_bytes(), raw)

    def test_legacy_receipts_reject_changed_controller_fields_or_context(self):
        for phase, change in (
            ("plan", "base"),
            ("proposal", "route"),
            ("plan", "context"),
            ("proposal", "context"),
        ):
            with self.subTest(phase=phase, change=change):
                self.root = self.root.parent / f"{phase}-{change}"
                study, harness, state, key, attempt = self.legacy_completed_call(phase)
                context = read(attempt / "context.json")
                receipt = read(attempt / "completed.json")
                if change == "base":
                    receipt["response"]["base_experiment"] = None
                elif change == "route":
                    receipt["response"]["route_id"] = "new"
                else:
                    context["goal"] = "A different frozen question"
                (attempt / "completed.json").write_text(json.dumps(receipt))
                raw = (attempt / "completed.json").read_bytes()
                calls, count = len(harness.calls), state["call_count"]
                with self.assertRaises((StudyError, ValueError)):
                    Study(self.root, harness)._call(
                        state, phase, key, context, attempt.parent
                    )
                self.assertEqual(len(harness.calls), calls)
                self.assertEqual(state["call_count"], count)
                self.assertEqual((attempt / "completed.json").read_bytes(), raw)
                self.assertEqual(harness.count("execute"), 1)

    def test_readonly_retry_rejects_execute_under_state_lock(self):
        study, harness = self.study()

        def fail(call):
            if call["phase"] == "execute":
                raise HarnessError("Unresolved execution")

        harness.before = fail
        with self.assertRaises(HarnessError):
            study.run()
        before = study.status()
        with self.assertRaisesRegex(StudyError, "read-only phase"):
            study.retry(readonly=True)
        self.assertEqual(study.status(), before)

    def test_two_iterations_keep_route_snapshots_and_earlier_graph_references(self):
        study, harness = self.study()
        harness.stop_after = 2
        first_state = study.run()
        first = self.root / "iterations/0001"
        route_id = first_state["route_id"]
        harness.base = "0001"
        state = study.run()
        second = self.root / "iterations/0002"
        self.assertEqual((state["status"], state["stop_reason"]), ("stopped", "review"))
        self.assertEqual(len(harness.calls), 20)
        sampled = next(
            c
            for c in harness.calls
            if c["phase"] == "proposal" and c["context"]["route"]["id"] == route_id
        )
        self.assertNotIn("route_id", sampled["schema"]["properties"])
        self.assertNotIn("route_id", read(sampled["run_dir"] / "response.json"))
        selection = read(second / "selection.json")
        side = sampled["context"]["role"]
        self.assertEqual(
            selection["rounds"]["0"]["proposals"][side]["route_id"], route_id
        )
        self.assertEqual(
            (second / "context/global.before.md").read_text(),
            (first / "global.after.md").read_text(),
        )
        self.assertEqual(
            (second / "context/route.before.md").read_text(),
            (first / "route.after.md").read_text(),
        )
        graph = study.graph()
        self.assertEqual(graph[1]["route_id"], route_id)
        self.assertEqual(graph[1]["base_experiment"], "0001")
        plan_call = [c for c in harness.calls if c["phase"] == "plan"][-1]
        self.assertNotIn("base_experiment", plan_call["schema"]["properties"])
        self.assertNotIn(
            "base_experiment", read(plan_call["run_dir"] / "response.json")
        )
        self.assertEqual(Path(state["plan_ref"]).name, "canonical-response.json")
        self.assertEqual(read(self.root / state["plan_ref"])["base_experiment"], "0001")
        self.assertEqual(graph[1]["references"], ["0001"])
        execute_call = [c for c in harness.calls if c["phase"] == "execute"][-1]
        self.assertEqual(execute_call["timeout_seconds"], 60)
        self.assertNotIn("input_artifacts", graph[1])
        self.assertNotIn("contract", execute_call["context"]["plan"])
        self.assertEqual(graph[0]["status"], "reviewed")
        result = read(self.root / state["review_ref"])
        self.assertNotIn("hypothesis_status", result)
        self.assertNotIn("score", result)
        self.assertEqual(result["goal_status"], "undetermined")
        self.assertTrue((first / "context/frontiers.before.json").exists())

    def test_empty_selection_stop_never_executes_or_registers_route(self):
        study, harness = self.study()
        harness.judge_decision = "stop"
        state = study.run(iterations=3)
        self.assertEqual(state["stop_reason"], "No feasible investigation")
        self.assertEqual(harness.count("proposal"), 2)
        self.assertEqual(harness.count("execute"), 0)
        self.assertEqual(read(self.root / "routes/index.json"), {})
        self.assertEqual(state["goal_status"], "undetermined")

    def test_resampling_is_bounded_and_does_not_claim_completion(self):
        study, harness = self.study(selection={"max_resamples": 1})
        harness.judge_decision = "resample"
        state = study.run()
        self.assertEqual(state["stop_reason"], "selection_budget")
        self.assertEqual(harness.count("judge"), 2)
        self.assertEqual(len(harness.calls), 14)
        self.assertEqual(harness.count("plan"), 0)
        self.assertEqual(state["goal_status"], "undetermined")

    def test_successful_execution_receipt_recovers_without_reexecution(self):
        study, harness = self.study()

        def crash(call):
            if call["phase"] == "execute":
                raise ProcessLost()

        harness.after = crash
        with self.assertRaises(ProcessLost):
            study.run()
        harness.after = None
        self.assertEqual(Study(self.root, harness).run()["status"], "stopped")
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(harness.count("review"), 1)

    def test_contract_change_stops_before_review_without_repeating_execution(self):
        study, harness = self.study()

        def change_contract(call):
            if call["phase"] == "execute":
                (self.root / "goal.md").write_text("A different objective")

        harness.after = change_contract
        with self.assertRaisesRegex(StudyError, "goal changed"):
            study.run()
        self.assertEqual(harness.count("review"), 0)
        self.assertFalse((self.root / "iterations/0001/publication.json").exists())
        (self.root / "goal.md").write_text(
            (self.root / "iterations/0001/context/goal.md").read_text()
        )
        harness.after = None
        study.retry()
        self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(harness.count("execute"), 1)

    def test_unknown_execution_requires_certification_and_preserves_old_attempt(self):
        study, harness = self.study()

        def fail(call):
            if call["phase"] == "execute":
                (call["run_dir"] / "partial.txt").write_text(
                    "Effects require inspection."
                )
                raise HarnessError("Execution state unknown")

        harness.before = fail
        with self.assertRaises(HarnessError):
            study.run()
        self.assertEqual(study.status()["phase"], "execute")
        with self.assertRaisesRegex(StudyError, "no action was repeated"):
            study.run()
        self.assertEqual(harness.count("execute"), 1)
        harness.before = None
        with self.assertRaisesRegex(StudyError, "trusted reconciliation"):
            study.retry()
        retained = study.status()
        study.reconcile_execution(
            recovery_id="verified-unsubmitted-execute",
            certify=lambda state: {
                "source_iteration": "0001",
                "source_attempt": retained["calls"]["execute"],
                "plan_ref": retained["plan_ref"],
                "action": "retry",
                "effect": "never_submitted",
                "summary": "Trusted fixture backend confirms no execution was submitted.",
                "proof_refs": ["backend/no-submission.json"],
            },
        )
        study.run()
        folder = self.root / "iterations/0001/execute"
        self.assertEqual(
            (folder / "attempt-001/partial.txt").read_text(),
            "Effects require inspection.",
        )
        self.assertTrue((folder / "attempt-002/process.json").exists())
        self.assertEqual(harness.count("execute"), 2)

    def test_review_conflict_preserves_human_edit_and_retry_reads_it(self):
        study, harness = self.study()

        def edit(call):
            if call["phase"] == "review":
                (self.root / "research.md").write_text(
                    "Human correction with contrary evidence."
                )

        harness.after = edit
        with self.assertRaisesRegex(StudyError, "changed during review"):
            study.run()
        self.assertEqual(
            (self.root / "research.md").read_text(),
            "Human correction with contrary evidence.",
        )
        self.assertFalse((self.root / "iterations/0001/publication.json").exists())
        harness.after = None
        study.retry()
        study.run()
        reviews = [c for c in harness.calls if c["phase"] == "review"]
        self.assertEqual(
            reviews[-1]["context"]["global_research"],
            "Human correction with contrary evidence.",
        )
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(len(reviews), 2)

    def test_prepared_publication_repairs_partial_writes_without_model_call(self):
        study, harness = self.study()
        from research_loop import core

        original = core._atomic_text
        failed = False

        def crash(path, value):
            nonlocal failed
            if (
                path == self.root / "frontiers.json"
                and (self.root / "iterations/0001/publication.json").exists()
                and not failed
            ):
                failed = True
                raise ProcessLost()
            original(path, value)

        with (
            patch.object(core, "_atomic_text", side_effect=crash),
            self.assertRaises(ProcessLost),
        ):
            study.run()
        self.assertTrue((self.root / "iterations/0001/publication.json").exists())
        calls = len(harness.calls)
        recovered = Study(self.root, harness)
        self.assertEqual(recovered.run()["status"], "stopped")
        self.assertEqual(len(harness.calls), calls)
        self.assertEqual(recovered.graph()[0]["status"], "reviewed")
        self.assertEqual(
            (self.root / "research.md").read_text(),
            (self.root / "iterations/0001/global.after.md").read_text(),
        )

    def test_selector_resume_reuses_proposal_and_frozen_sampling(self):
        study, harness = self.study()

        def crash(call):
            if call["phase"] == "proposal":
                raise ProcessLost()

        harness.after = crash
        with self.assertRaises(ProcessLost):
            study.run()
        selection = read(self.root / "iterations/0001/selection.json")
        harness.after = None
        study.run()
        self.assertEqual(harness.count("proposal"), 2)
        resumed = read(self.root / "iterations/0001/selection.json")
        self.assertEqual(
            resumed["rounds"]["0"]["candidates"], selection["rounds"]["0"]["candidates"]
        )
        self.assertEqual(study.status()["sample_counter"], 1)

    def test_only_execute_can_write_and_historical_code_is_restored(self):
        study, harness = self.study(allow_write=True, max_iterations=3)
        harness.stop_after = 3
        harness.write_code = True
        study.run()
        harness.base = "0001"
        study.run()
        study.run()
        self.assertEqual(
            harness.execute_inputs, [None, "version = 1\n", "version = 1\n"]
        )
        for call in harness.calls:
            self.assertEqual(call["writable"], call["phase"] == "execute")
        self.assertEqual(
            (self.root / "iterations/0001/workspace/model.py").read_text(),
            "version = 1\n",
        )
        self.assertEqual(
            (self.root / "iterations/0002/workspace/model.py").read_text(),
            "version = 2\n",
        )
        self.assertFalse((study.workspace / "model.py").exists())

    def test_global_and_local_frontiers_bind_exact_reviewed_versions(self):
        study, harness = self.study(global_evaluation=evaluation("global-v1"))
        harness.local = evaluation("local-v1")
        harness.measure = True
        state = study.run()
        views = study.frontiers()
        global_view = views["global"]["global-v1"]
        local_view = views["local"][state["route_id"]]["local-v1"]
        self.assertEqual(global_view["candidate_ids"], ["0001:global-v1"])
        self.assertEqual(local_view["candidate_ids"], ["0001:local-v1"])
        record = global_view["candidates"][0]
        self.assertEqual(record["artifact"]["version"], "v1")
        self.assertEqual(record["metrics"], {"quality": 0.9, "cost": 2})
        self.assertEqual(record["experiment_id"], "0001")
        self.assertEqual(study.rebuild_frontiers(), views)

    def test_final_only_measurements_are_rejected_before_review(self):
        final = evaluation("final-v1")
        final.update(data_role="final", allow_feedback=False)
        study, harness = self.study(global_evaluation=final)
        harness.measure = True
        with self.assertRaisesRegex(ValueError, "measurements"):
            study.run()
        self.assertEqual(study.status()["phase"], "execute")
        self.assertEqual(harness.count("review"), 0)
        self.assertEqual(study.frontiers()["global"], {})

    def test_saved_final_only_view_is_hidden_from_search(self):
        final = evaluation("final-v1")
        final.update(data_role="final", allow_feedback=False)
        study, harness = self.study(global_evaluation=final)
        (self.root / "frontiers.json").write_text(
            json.dumps(
                {
                    "global": {
                        "final-v1": {
                            "candidate_ids": ["secret"],
                            "allow_feedback": False,
                            "data_role": "final",
                        }
                    },
                    "local": {},
                }
            )
        )
        study.run()
        search = [
            call
            for call in harness.calls
            if call["phase"] in ("proposal", "critique", "response", "judge", "plan")
        ]
        self.assertTrue(search)
        self.assertTrue(
            all(call["context"]["frontiers"]["global"] == {} for call in search)
        )

    def test_checkpoint_failure_retry_reuses_successful_execute(self):
        study, harness = self.study(allow_write=True)
        harness.write_code = True
        checkpoint = study._checkpoint
        failed = False

        def fail_once(*args):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("Temporary checkpoint publication failure")
            return checkpoint(*args)

        with patch.object(study, "_checkpoint", side_effect=fail_once):
            with self.assertRaises(OSError):
                study.run()
            self.assertEqual(harness.count("execute"), 1)
            study.retry()
            self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(harness.count("execute"), 1)
        self.assertEqual(study.graph()[0]["status"], "reviewed")

    def test_publication_oserror_is_replayed_without_another_model_call(self):
        from research_loop import core

        study, harness = self.study()
        original = core._atomic_text
        failed = False

        def fail_once(path, value):
            nonlocal failed
            if (
                path == self.root / "research.md"
                and (self.root / "iterations/0001/publication.json").exists()
                and not failed
            ):
                failed = True
                raise OSError("Temporary publication failure")
            original(path, value)

        with patch.object(core, "_atomic_text", side_effect=fail_once):
            with self.assertRaises(OSError):
                study.run()
            call_count = len(harness.calls)
            self.assertEqual(study.run()["status"], "stopped")
        self.assertEqual(len(harness.calls), call_count)
        self.assertEqual(study.graph()[0]["status"], "reviewed")

    def test_unlisted_source_dependency_is_restored_with_checkpoint(self):
        study, harness = self.study(allow_write=True)
        harness.stop_after = 2
        harness.write_code = True
        harness.write_helper = True
        study.run()
        harness.base = "0001"
        harness.write_helper = False
        study.run()
        self.assertEqual(harness.helper_inputs, [None, "def helper():\n    return 1\n"])
        execution = read(
            self.root / "iterations/0001/execute/attempt-001/response.json"
        )
        self.assertNotIn(
            "helper.py", [artifact["path"] for artifact in execution["artifacts"]]
        )

    def test_local_evaluation_names_are_scoped_to_route(self):
        study, harness = self.study(global_evaluation=evaluation("global-v1"))
        harness.stop_after = 2
        harness.measure = True
        harness.local = evaluation("local-v1")
        first_route = study.run()["route_id"]
        harness.choose_new = True
        harness.local = evaluation("local-v1")
        harness.local["objectives"][0]["direction"] = "minimize"
        second_route = study.run()["route_id"]
        self.assertNotEqual(first_route, second_route)
        views = study.frontiers()["local"]
        self.assertEqual(
            views[first_route]["local-v1"]["candidate_ids"], ["0001:local-v1"]
        )
        self.assertEqual(
            views[second_route]["local-v1"]["candidate_ids"], ["0002:local-v1"]
        )
        self.assertEqual(study.rebuild_frontiers()["local"], views)

    def test_review_correction_resurrects_historical_candidate_without_rewriting_it(
        self,
    ):
        study, harness = self.study(global_evaluation=evaluation("global-v1"))
        harness.stop_after = 3
        harness.measure = True
        harness.quality = 0.8
        study.run()
        harness.quality = 0.9
        study.run()
        self.assertEqual(
            study.frontiers()["global"]["global-v1"]["candidate_ids"],
            ["0002:global-v1"],
        )
        original_path = self.root / "iterations/0002/candidates.json"
        original = original_path.read_text()
        harness.measure = False
        harness.corrections = [
            {
                "candidate_id": "0002:global-v1",
                "valid": False,
                "verified": True,
                "reason": "New evidence identifies split contamination in the earlier evaluation.",
            }
        ]
        study.run()
        corrected = study.frontiers()["global"]["global-v1"]
        self.assertEqual(corrected["candidate_ids"], ["0001:global-v1"])
        self.assertIn(
            {"id": "0002:global-v1", "reason": "invalid"}, corrected["excluded"]
        )
        self.assertEqual(original_path.read_text(), original)
        self.assertTrue(read(original_path)["candidates"][0]["valid"])
        self.assertIn("0002", study.graph()[2]["references"])
        self.assertEqual(study.rebuild_frontiers()["global"]["global-v1"], corrected)

    def test_idle_route_edit_is_snapshotted_without_rewriting_previous_publication(
        self,
    ):
        study, harness = self.study()
        harness.stop_after = 2
        first = study.run()
        route_id = first["route_id"]
        old_path = self.root / "iterations/0001/route.after.md"
        old_text = old_path.read_text()
        edited = (
            "Human correction: the former interpretation lacks supporting evidence."
        )
        (self.root / f"routes/{route_id}/research.md").write_text(edited)
        previous_calls = len(harness.calls)
        study.run()
        proposal = next(
            call
            for call in harness.calls[previous_calls:]
            if call["phase"] == "proposal"
            and call["context"]["route"]["id"] == route_id
        )
        self.assertEqual(proposal["context"]["route"]["research"], edited)
        self.assertEqual(old_path.read_text(), old_text)
        self.assertEqual(
            (self.root / "iterations/0002/context/route.before.md").read_text(), edited
        )

    def test_future_graph_reference_cannot_reach_execution(self):
        study, harness = self.study()
        harness.base = "0001"
        with self.assertRaisesRegex(StudyError, "earlier reviewed"):
            study.run()
        self.assertEqual(harness.count("execute"), 0)
        self.assertEqual(study.status()["status"], "needs_attention")
        self.assertEqual(read(self.root / "routes/index.json"), {})

    def test_lock_rejects_second_controller_before_harness_call(self):
        study, harness = self.study()
        with study._lock(), self.assertRaisesRegex(StudyError, "Another controller"):
            Study(self.root, harness).run()
        self.assertEqual(harness.calls, [])

    def test_workspace_cannot_contain_control_records(self):
        with self.assertRaisesRegex(ValueError, "must not contain"):
            initialize(self.root, goal="A goal", workspace=self.root.parent)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
