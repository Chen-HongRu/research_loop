import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from research_loop import Study, initialize
from research_loop.protocol import SCHEMAS
from research_loop.long_training import (
    LongTrainingManager,
    completed_results,
    snapshot,
    validate_policy,
    stage_schema,
    validate_observation_reviews,
    observation_context,
)
from test_core import RecordedHarness, evaluation


def read(path):
    return json.loads(path.read_text())


def policy():
    long_evaluation = evaluation("long")
    long_evaluation["comparison_scope"] = "full-10k"
    return {
        "version": 1,
        "recipe": {
            "total_steps": 10000,
            "validation_interval": 2000,
            "validation_split": "full",
            "initialization": "fresh",
            "seed": 42,
        },
        "evaluation": long_evaluation,
        "max_runs": 2,
        "max_seconds_per_run": 1000,
    }


class ParentHarness(RecordedHarness):
    def __init__(self):
        super().__init__()
        self.measure = self.write_code = True
        self.stop_after = 10
        self.nominate = True
        self.basis = "significant_frontier_gain"

    def run(self, **kwargs):
        if "long_training_decision" in kwargs["schema"]["properties"]:
            kwargs = {**kwargs, "schema": SCHEMAS["review"]}
        return super().run(**kwargs)

    def response(self, phase, context, kwargs):
        if phase == "execute":
            (Path(kwargs["workspace"]) / "result.json").write_text("{}")
        result = super().response(phase, context, kwargs)
        if phase == "execute":
            for artifact in result["artifacts"]:
                if artifact["path"] == "result.json":
                    artifact["version"] = "source-" + context["experiment"]["id"]
            for measurement in result["measurements"]:
                measurement["artifact"]["version"] = (
                    "source-" + context["experiment"]["id"]
                )
        if phase == "review" and context.get("long_training"):
            result["long_training_decision"] = {
                "reason": "Longer full-validation trajectory resolves the short-budget uncertainty.",
                "nominations": [],
            }
            if context["experiment"]["id"] == "0002" and self.nominate:
                result["long_training_decision"]["nominations"] = [
                    {
                        "candidate_id": "short",
                        "comparison_candidate_id": "0001:short",
                        "basis": self.basis,
                        "reason": "Material representation change with measured improvement; mechanism remains unproved.",
                        "evidence": [
                            "iterations/0002/execute.md",
                            "iterations/0001/execute.md",
                        ],
                    }
                ]
        return result


class LongHarness(RecordedHarness):
    """Adapt only this fixture to the opt-in child Review extension."""

    def run(self, **kwargs):
        if "observation_reviews" in kwargs["schema"]["properties"]:
            kwargs = {**kwargs, "schema": SCHEMAS["review"]}
        return super().run(**kwargs)

    def response(self, phase, context, kwargs):
        result = super().response(phase, context, kwargs)
        if phase == "plan":
            result = {key: result[key] for key in kwargs["schema"]["properties"]}
            result["contract"] = {
                key: result["contract"][key]
                for key in kwargs["schema"]["properties"]["contract"]["properties"]
            }
        if phase == "review" and context.get("long_training_run"):
            observations = context["long_training_observations"]
            result["observation_reviews"] = [
                {
                    "step": point["step"],
                    "valid": True,
                    "verified": True,
                    "reason": "Checked complete full-validation checkpoint evidence.",
                }
                for point in observations["curve"]
            ]
            knowledge = (
                "Complete full-validation observations at steps "
                + str([point["step"] for point in observations["curve"]])
                + "; endpoint_complete="
                + str(observations["endpoint_complete"])
                + "; missing_validation_steps="
                + str(observations["missing_validation_steps"])
                + ". Timeout does not establish ineffectiveness; endpoint and mechanism remain uncertain."
            )
            result.update(
                markdown=knowledge,
                global_research=knowledge,
                knowledge_changes=[knowledge],
            )
        return result


class LongTrainingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "parent"
        initialize(
            self.root,
            goal="Measure practical quality",
            allow_write=True,
            global_evaluation=evaluation("short"),
            long_training=policy(),
        )
        self.harness = ParentHarness()
        self.study = Study(self.root, self.harness)

    def nominate(self):
        self.harness.quality = 0.8
        self.study.run()
        self.harness.base = "0001"
        self.harness.quality = 0.9
        self.study.run()
        return read(self.root / "iterations/0002/long-training.json")["requests"][0]

    def executor(self, calls, *, fail=False, partial=False):
        def execute(request, workspace, *, deadline):
            calls.append(request["id"])
            if fail:
                raise RuntimeError("Unknown remote effects")
            workspace.mkdir(parents=True, exist_ok=True)
            artifact = {
                "path": "result.json",
                "version": request["candidate"]["artifact"]["version"],
            }
            (workspace / "result.json").write_text(
                json.dumps(
                    {
                        "run_id": request["id"],
                        "source_identity": artifact["version"],
                        "source_candidate": request["candidate"],
                        "evaluation": request["evaluation"],
                        "recipe": request["recipe"],
                        "curve": [
                            {
                                "step": step,
                                "proteins": 4491,
                                "quality": 0.95,
                                "cost": 20,
                                "checkpoint": f"long-checkpoint:{request['id']}:{step}",
                                "evidence": [f"full-validation/step-{step}/eval.csv"],
                            }
                            for step in (
                                [2000, 4000, 6000]
                                if partial
                                else [2000, 4000, 6000, 8000, 10000]
                            )
                        ],
                        "endpoint_complete": not partial,
                        "missing_validation_steps": [8000, 10000] if partial else [],
                        "log_evidence": ["run.log"],
                    }
                )
            )
            return {
                "outcome": "incomplete" if partial else "completed",
                "markdown": "Partial full-validation trajectory; missing 10k endpoint."
                if partial
                else "Measured complete full-development endpoint and checkpoint.",
                "artifacts": [artifact],
                "measurements": []
                if partial
                else [
                    {
                        "candidate_id": "endpoint",
                        "artifact": artifact,
                        "evaluation_id": request["evaluation"]["id"],
                        "comparison_scope": request["evaluation"]["comparison_scope"],
                        "metrics": [
                            {"name": "quality", "value": 0.95},
                            {"name": "cost", "value": 20},
                        ],
                        "evidence": [str(workspace / "result.json")],
                    }
                ],
            }

        return execute

    def test_real_child_study_and_boundary_feedback(self):
        request = self.nominate()
        calls = []
        delegate = LongHarness()
        manager = LongTrainingManager(self.root, delegate, self.executor(calls))
        result = manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(calls, [request["id"]])
        self.assertEqual([c["phase"] for c in delegate.calls], ["plan", "review"])
        self.assertEqual(result["recipe"]["initialization"], "fresh")
        self.assertEqual(snapshot(self.root)[0]["status"], "reviewed")
        child = self.root / "long-runs" / request["id"] / "study"
        self.assertTrue((child / "iterations/0001/publication.json").is_file())
        self.assertEqual(
            Study(child).frontiers()["global"]["long"]["candidate_ids"],
            ["0001:endpoint"],
        )
        self.assertEqual(
            self.study.frontiers()["global"]["short"]["candidate_ids"], ["0002:short"]
        )
        manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(len(calls), 1)
        self.harness.base = "0002"
        self.study.run()
        frozen = read(self.root / "iterations/0003/context/long_training_results.json")
        self.assertEqual(frozen, completed_results(self.root))
        review = next(c for c in reversed(self.harness.calls) if c["phase"] == "review")
        self.assertEqual(
            review["context"]["long_training_results"][0]["id"], request["id"]
        )

    def test_incomplete_real_child_reviews_points_and_feeds_next_parent_boundary(self):
        request = self.nominate()
        calls = []
        delegate = LongHarness()
        result = LongTrainingManager(
            self.root, delegate, self.executor(calls, partial=True)
        ).run_one(deadline=time.time() + 1000)
        self.assertEqual(result["execute"]["outcome"], "incomplete")
        self.assertEqual(result["execute"]["measurements"], [])
        self.assertEqual(
            [row["step"] for row in result["review"]["observation_reviews"]],
            [2000, 4000, 6000],
        )
        self.assertEqual(result["review"]["candidate_reviews"], [])
        child = self.root / "long-runs" / request["id"] / "study"
        self.assertTrue((child / "iterations/0001/publication.json").exists())
        self.assertEqual(
            Study(child).frontiers()["global"]["long"]["candidate_ids"], []
        )
        self.assertIn(
            "missing_validation_steps=[8000, 10000]",
            (child / "research.md").read_text(),
        )
        self.assertEqual(
            self.study.frontiers()["global"]["short"]["candidate_ids"], ["0002:short"]
        )
        original = self.harness.response

        def preserve(phase, context, kwargs):
            response = original(phase, context, kwargs)
            if phase == "review" and context.get("long_training_results"):
                observed = context["long_training_results"][0]
                response["global_research"] += (
                    "\n" + observed["review"]["global_research"]
                )
                response["knowledge_changes"].append(
                    "Partial long trajectory retained; no endpoint measured."
                )
            return response

        self.harness.response = preserve
        self.harness.base = "0002"
        self.study.run()
        frozen = read(self.root / "iterations/0003/context/long_training_results.json")
        self.assertEqual(frozen[0]["result"]["missing_validation_steps"], [8000, 10000])
        self.assertIn(
            "endpoint_complete=False", (self.root / "research.md").read_text()
        )
        self.assertEqual(frozen[0]["execute"]["measurements"], [])

    def test_observation_acceptance_rejects_missing_duplicate_and_invented_steps(self):
        request = self.nominate()
        delegate = LongHarness()
        result = LongTrainingManager(
            self.root, delegate, self.executor([], partial=True)
        ).run_one(deadline=time.time() + 1000)
        context = next(
            call["context"] for call in delegate.calls if call["phase"] == "review"
        )
        rows = result["review"]["observation_reviews"]
        for altered in (
            rows[:-1],
            rows + [rows[0]],
            rows
            + [{"step": 10000, "valid": True, "verified": True, "reason": "Invented"}],
        ):
            with self.subTest(steps=[row["step"] for row in altered]):
                with self.assertRaisesRegex(ValueError, "match complete curve"):
                    validate_observation_reviews(
                        context, {"observation_reviews": altered}
                    )
        blank = copy.deepcopy(rows)
        blank[0]["reason"] = " "
        with self.assertRaisesRegex(ValueError, "concrete reason"):
            validate_observation_reviews(context, {"observation_reviews": blank})
        path = Path(context["experiment"]["workspace"]) / "result.json"
        value = read(path)
        value["source_identity"] = "changed-source"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "frozen request"):
            observation_context(context)
        self.assertEqual(request["id"], result["id"])

    def test_observation_schema_is_opt_in_and_parent_nomination_contract_stays(self):
        self.assertIs(stage_schema("review", {}, SCHEMAS["review"]), SCHEMAS["review"])
        parent = stage_schema("review", {"long_training": policy()}, SCHEMAS["review"])
        self.assertNotIn("observation_reviews", parent["properties"])
        child = stage_schema(
            "review",
            {
                "long_training_run": {"id": "frozen"},
                "long_training_observations": {"curve": [{"step": 5}]},
            },
            SCHEMAS["review"],
        )
        self.assertIn("observation_reviews", child["required"])
        self.assertNotIn("long_training_decision", child["properties"])

    def test_published_partial_result_uses_reviewed_context_after_live_mutation(self):
        request = self.nominate()
        delegate = LongHarness()

        def mutate_after_review(call):
            if call["phase"] == "review":
                path = Path(call["context"]["experiment"]["workspace"]) / "result.json"
                live = read(path)
                live["curve"][0]["quality"] = 0.01
                path.write_text(json.dumps(live))

        delegate.after = mutate_after_review
        result = LongTrainingManager(
            self.root, delegate, self.executor([], partial=True)
        ).run_one(deadline=time.time() + 1000)
        live = read(
            self.root / "long-runs" / request["id"] / "study/workspace/result.json"
        )
        self.assertEqual(live["curve"][0]["quality"], 0.01)
        self.assertEqual(result["result"]["curve"][0]["quality"], 0.95)
        self.assertEqual(completed_results(self.root)[0]["result"], result["result"])
        self.assertEqual(result["result"]["missing_validation_steps"], [8000, 10000])
        self.assertEqual(result["execute"]["measurements"], [])

    def test_frozen_context_does_not_change_when_child_completes_mid_iteration(self):
        request = self.nominate()
        self.harness.base = "0002"
        manager = LongTrainingManager(self.root, LongHarness(), self.executor([]))

        def finish(call):
            if call["phase"] == "plan":
                manager.run_one(deadline=time.time() + 1000)

        self.harness.before = finish
        self.study.run()
        self.assertEqual(
            read(self.root / "iterations/0003/context/long_training_results.json"), []
        )
        self.assertEqual(completed_results(self.root)[0]["id"], request["id"])

    def test_unchanged_candidate_cannot_be_nominated(self):
        self.harness.quality = 0.9
        self.study.run()
        self.harness.base = "0001"
        with self.assertRaisesRegex(ValueError, "Unchanged"):
            self.study.run()
        self.assertFalse((self.root / "iterations/0002/publication.json").exists())

    def test_invalid_review_candidate_cannot_be_nominated(self):
        self.harness.quality = 0.8
        self.study.run()
        self.harness.base = "0001"
        self.harness.quality = 0.9
        original = self.harness.response

        def invalidate(phase, context, kwargs):
            result = original(phase, context, kwargs)
            if phase == "review":
                result["candidate_reviews"][0]["valid"] = False
            return result

        self.harness.response = invalidate
        with self.assertRaisesRegex(ValueError, "ineligible"):
            self.study.run()

    def test_unknown_execute_is_never_repeated(self):
        self.nominate()
        calls = []
        manager = LongTrainingManager(
            self.root, LongHarness(), self.executor(calls, fail=True)
        )
        with self.assertRaisesRegex(RuntimeError, "Unknown"):
            manager.run_one(deadline=time.time() + 1000)
        with self.assertRaises(Exception):
            manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(completed_results(self.root), [])

    def test_queued_candidate_is_withdrawn_after_eligibility_correction(self):
        request = self.nominate()
        manager = LongTrainingManager(self.root, LongHarness(), self.executor([]))
        manager.sync_requests()
        self.harness.base = "0002"
        self.harness.corrections = [
            {
                "candidate_id": "0002:short",
                "valid": False,
                "verified": True,
                "reason": "New evidence invalidates this source; see original and corrected receipts.",
            }
        ]
        self.study.run()
        self.assertIsNone(manager.run_one(deadline=time.time() + 1000))
        self.assertEqual(snapshot(self.root)[0]["status"], "withdrawn")
        self.assertFalse(
            (self.root / "long-runs" / request["id"] / "study/study.json").exists()
        )

    def test_started_child_drains_after_cutoff_and_does_not_admit_queue(self):
        request = self.nominate()
        calls = []
        delegate = LongHarness()
        clock = [100.0]

        def cross_cutoff(call):
            if call["phase"] == "plan":
                clock[0] = 201.0

        delegate.before = cross_cutoff
        manager = LongTrainingManager(self.root, delegate, self.executor(calls))
        with patch(
            "research_loop.long_training.time.time", side_effect=lambda: clock[0]
        ):
            result = manager.run_one(deadline=200.0)
            self.assertIsNone(manager.run_one(deadline=200.0))
        self.assertEqual(result["id"], request["id"])
        self.assertEqual(calls, [request["id"]])
        self.assertEqual([c["phase"] for c in delegate.calls], ["plan", "review"])

    def prepare_child(self):
        request = self.nominate()
        manager = LongTrainingManager(self.root, LongHarness(), self.executor([]))
        manager.sync_requests()
        child = self.root / "long-runs" / request["id"] / "study"
        initialize(
            child,
            goal="Prepared frozen child",
            global_evaluation=request["evaluation"],
            max_iterations=1,
            max_calls=12,
            max_seconds=policy()["max_seconds_per_run"],
            strategy={"selection": "direct", "review": "full"},
        )
        return request, child

    def test_initialized_child_can_enter_before_cutoff_without_reinitializing(self):
        request, child = self.prepare_child()
        original_config = (child / "study.json").read_bytes()
        calls = []
        delegate = LongHarness()
        manager = LongTrainingManager(self.root, delegate, self.executor(calls))
        result = manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(result["id"], request["id"])
        self.assertEqual(calls, [request["id"]])
        self.assertEqual([call["phase"] for call in delegate.calls], ["plan", "review"])
        self.assertEqual(
            read(child / "study.json"),
            json.loads(original_config) | {"long_training_run": request},
        )

    def test_initialized_child_is_not_active_after_cutoff_or_without_admission(self):
        _, child = self.prepare_child()
        initial_state = read(child / "state.json")
        calls = []
        delegate = LongHarness()
        manager = LongTrainingManager(self.root, delegate, self.executor(calls))
        self.assertIsNone(manager.run_one(deadline=0))
        self.assertIsNone(manager.run_one(deadline=time.time() + 1000, allow_new=False))
        self.assertEqual(read(child / "state.json"), initial_state)
        self.assertEqual(calls, [])
        self.assertEqual(delegate.calls, [])

    def test_initialized_child_rechecks_admission_authorization(self):
        _, child = self.prepare_child()
        calls = []
        delegate = LongHarness()

        def blocked(request, token):
            raise RuntimeError("Long-training GPU budget exhausted or HANDOFF")

        manager = LongTrainingManager(
            self.root, delegate, self.executor(calls), admission=blocked
        )
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(read(child / "state.json")["iteration"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(delegate.calls, [])

    def test_initialized_child_rechecks_source_eligibility(self):
        request, child = self.prepare_child()
        frontiers = read(self.root / "frontiers.json")
        view = frontiers["global"][request["candidate"]["evaluation_id"]]
        view["candidate_ids"] = []
        view["excluded"] = [{"id": request["candidate"]["id"], "reason": "invalid"}]
        (self.root / "frontiers.json").write_text(json.dumps(frontiers))
        calls = []
        delegate = LongHarness()
        manager = LongTrainingManager(self.root, delegate, self.executor(calls))
        self.assertIsNone(manager.run_one(deadline=time.time() + 1000))
        self.assertEqual(snapshot(self.root)[0]["status"], "withdrawn")
        self.assertEqual(read(child / "state.json")["iteration"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(delegate.calls, [])

    def test_fixed_controller_freezes_gpu_timeout_and_keeps_agent_timeout(self):
        self.nominate()
        delegate = LongHarness()
        calls = []
        manager = LongTrainingManager(
            self.root,
            delegate,
            self.executor(calls),
            admission=lambda request, token: {"policy": {"gpu_seconds_per_run": 60}},
        )
        result = manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(result["execute"]["outcome"], "completed")
        plan_call = next(call for call in delegate.calls if call["phase"] == "plan")
        self.assertIn("Slurm GPU allocation time, exactly 60", plan_call["prompt"])
        self.assertEqual(len(calls), 1)

    def test_authorization_stays_out_of_child_artifacts_and_review_prompt(self):
        request = self.nominate()
        identity = {
            "owner": {"owner_token": "secret-owner-value"},
            "authorization": "secret-grant-text",
        }
        delegate = LongHarness()
        manager = LongTrainingManager(
            self.root,
            delegate,
            self.executor([]),
            admission=lambda request, token: identity,
        )
        manager.run_one(deadline=time.time() + 1000)
        child = self.root / "long-runs" / request["id"] / "study"
        artifacts = [
            child / "state.json",
            child / "iterations/0001/iteration.json",
            child / "iterations/0001/publication.json",
        ]
        texts = [path.read_text() for path in artifacts]
        texts.extend(path.read_text() for path in child.rglob("prompt.md"))
        texts.extend(
            call["prompt"] for call in delegate.calls if call["phase"] == "review"
        )
        for text in texts:
            self.assertNotIn("owner_token", text)
            self.assertNotIn("secret-owner-value", text)
            self.assertNotIn("secret-grant-text", text)
            self.assertNotIn('"authorization"', text)
        from research_loop.long_training import admission_path

        record = read(admission_path(child.parent))
        self.assertEqual(record["authorization"], identity)
        token = read(child / "state.json")["round_admission"]
        self.assertEqual(set(token), {"version", "id", "nonce"})

    def test_opaque_token_keeps_identity_changes_blocked(self):
        self.nominate()
        identity = {"owner": {"owner_token": "first"}}
        delegate = LongHarness()
        calls = []

        def change_owner(call):
            if call["phase"] == "plan":
                identity["owner"]["owner_token"] = "replacement"

        delegate.before = change_owner
        manager = LongTrainingManager(
            self.root,
            delegate,
            self.executor(calls),
            admission=lambda request, token: copy.deepcopy(identity),
        )
        with self.assertRaisesRegex(ValueError, "identity changed"):
            manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(calls, [])

    def test_inconsistent_fixed_controller_plan_cannot_allocate(self):
        class InvalidPlan(LongHarness):
            def response(self, phase, context, kwargs):
                result = super().response(phase, context, kwargs)
                if phase == "plan":
                    result["contract"]["timeout_seconds"] = 60
                return result

        self.nominate()
        calls = []
        manager = LongTrainingManager(
            self.root,
            InvalidPlan(),
            self.executor(calls),
            admission=lambda request, token: {"policy": {"gpu_seconds_per_run": 10800}},
        )
        with self.assertRaisesRegex(ValueError, "contract: must contain exactly"):
            manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(calls, [])

    def test_prepared_token_cannot_commit_when_context_build_crosses_cutoff(self):
        from research_loop import core
        from research_loop.long_training import admitted_round

        request = self.nominate()
        manager = LongTrainingManager(self.root, LongHarness(), self.executor([]))
        clock = [100.0]
        atomic_text = core._atomic_text

        def crossing(path, content):
            result = atomic_text(path, content)
            if "long-runs" in path.parts and path.parent.name == "context":
                clock[0] = 201.0
            return result

        with (
            patch(
                "research_loop.long_training.time.time", side_effect=lambda: clock[0]
            ),
            patch.object(core, "_atomic_text", side_effect=crossing),
        ):
            with self.assertRaisesRegex(ValueError, "cutoff"):
                manager.run_one(deadline=200.0)
        folder = self.root / "long-runs" / request["id"]
        self.assertIsNone(admitted_round(folder))
        self.assertEqual(read(folder / "study/state.json")["iteration"], 0)

    def test_orphan_prepared_admission_cannot_start_after_cutoff(self):
        request = self.nominate()
        manager = LongTrainingManager(self.root, LongHarness(), self.executor([]))
        manager.sync_requests()
        folder = self.root / "long-runs" / request["id"]
        (folder / "round-admission.json").write_text(
            json.dumps({"entered_at": 1, "cutoff": 2})
        )
        self.assertIsNone(manager.run_one(deadline=0))
        self.assertFalse((folder / "study/study.json").exists())

    def test_final_only_and_resumed_recipes_rejected(self):
        invalid = copy.deepcopy(policy())
        invalid["recipe"]["initialization"] = "resume"
        with self.assertRaisesRegex(ValueError, "fresh"):
            validate_policy(invalid)
        invalid = copy.deepcopy(policy())
        invalid["evaluation"].update(data_role="final", allow_feedback=False)
        with self.assertRaisesRegex(ValueError, "development"):
            validate_policy(invalid)


if __name__ == "__main__":
    unittest.main()
