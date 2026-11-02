import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from research_loop import Study, initialize
from research_loop.protocol import SCHEMAS
from research_loop.long_training import (
    LongTrainingManager,
    completed_results,
    snapshot,
    validate_policy,
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

    def executor(self, calls, *, fail=False):
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
                        "recipe": request["recipe"],
                        "curve": [{"step": 10000, "quality": 0.95}],
                        "checkpoints": ["long-checkpoint:" + request["id"] + ":10000"],
                    }
                )
            )
            return {
                "outcome": "completed",
                "markdown": "Measured complete full-development endpoint and checkpoint.",
                "artifacts": [artifact],
                "measurements": [
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
        delegate = RecordedHarness()
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

    def test_frozen_context_does_not_change_when_child_completes_mid_iteration(self):
        request = self.nominate()
        self.harness.base = "0002"
        manager = LongTrainingManager(self.root, RecordedHarness(), self.executor([]))

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
            self.root, RecordedHarness(), self.executor(calls, fail=True)
        )
        with self.assertRaisesRegex(RuntimeError, "Unknown"):
            manager.run_one(deadline=time.time() + 1000)
        with self.assertRaises(Exception):
            manager.run_one(deadline=time.time() + 1000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(completed_results(self.root), [])

    def test_queued_candidate_is_withdrawn_after_eligibility_correction(self):
        request = self.nominate()
        manager = LongTrainingManager(self.root, RecordedHarness(), self.executor([]))
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
