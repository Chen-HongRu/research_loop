import copy
import json
from pathlib import Path
import tempfile
import unittest

from research_loop.meta import CONFIRMATION_CHECKS, MetaError, MetaStudy


def protocol(stage="confirmation"):
    return {
        "id": "fixed-suite-1",
        "stage": stage,
        "tasks": [
            {"id": "denoising-dirty-documents", "metric": "rmse"},
            {"id": "spooky-author-identification", "metric": "log_loss"},
        ]
        if stage == "confirmation"
        else [{"id": "aerial-cactus", "metric": "auc"}],
        "repeats": 3 if stage == "confirmation" else 1,
        "checks": sorted(CONFIRMATION_CHECKS),
        "run_budget": {"calls": 20, "seconds": 1800, "tokens": 1000000},
        "conditions": {
            key: "fixed"
            for key in (
                "model",
                "reasoning",
                "tools",
                "training",
                "environment",
                "task_states",
                "seed_plan",
                "order",
                "resources",
            )
        }
        | {"read_isolation": "enforced" if stage == "confirmation" else "unverified"},
    }


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        package = self.source / "src/research_loop"
        package.mkdir(parents=True)
        (package / "core.py").write_text("VALUE = 'baseline'\n")
        (self.source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        (self.source / "uv.lock").write_text("version = 1\n")
        self.study = self.initialize()
        self.serial = 0

    def tearDown(self):
        for path in self.base.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        self.temp.cleanup()

    def initialize(self, stage="confirmation", name="meta", budget=None):
        return MetaStudy.initialize(
            self.base / name,
            self.source,
            protocol(stage),
            {"selection": "debate", "review": "full"},
            budget,
        )

    def freeze(self, policy=None, name="candidate"):
        return self.study.freeze_candidate(
            policy or {"selection": "direct", "review": "concise"},
            self.study.status()["supervisor_ref"],
            name,
        )

    def report(self, candidate, stage="confirmation", **changes):
        state = self.study.status()
        tasks = (
            protocol()["tasks"]
            if stage == "confirmation"
            else [{"id": "aerial-cactus", "metric": "auc"}]
        )
        repeats = 3 if stage == "confirmation" else 1
        arms = {"S": candidate["parent_ref"], "C": candidate["ref"]}
        if stage == "confirmation" and state["a0_ref"] != candidate["parent_ref"]:
            arms["A0"] = state["a0_ref"]
        rows = []
        for arm, ref in arms.items():
            for task in tasks:
                for repeat in range(repeats):
                    self.serial += 1
                    job_id = f"job-{self.serial}"
                    row = {
                        "arm": arm,
                        "pair_id": f"{task['id']}:{repeat}",
                        "task": task["id"],
                        "repeat": repeat,
                        "job_id": job_id,
                        "overhead_seconds": 7 if arm == "C" else 10,
                        "total_seconds": 90 if arm == "C" else 100,
                        "tokens": 100,
                        "calls": 1,
                        "quality": 0.8 if stage == "development" else 1.0,
                        "valid": True,
                        "version_ref": ref,
                    }
                    if arm == "C":
                        row.update(changes)
                    self.study.reserve_job(
                        job_id,
                        ref,
                        2,
                        200,
                        1000,
                        evaluation={
                            "candidate_id": candidate["id"],
                            "stage": stage,
                            "arm": arm,
                            "task": task["id"],
                            "repeat": repeat,
                        },
                    )
                    self.study.finish_job(
                        job_id,
                        {
                            "status": "completed",
                            "calls": row["calls"],
                            "seconds": row["total_seconds"],
                            "tokens": row["tokens"],
                        },
                    )
                    rows.append(row)
        return {
            "stage": stage,
            "protocol_ref": state["protocol_ref"],
            "supervisor_ref": candidate["parent_ref"],
            "candidate_ref": candidate["ref"],
            "rows": rows,
            "protocol_checks": {key: True for key in protocol()["checks"]},
            "canary": {"S": True, "C": True},
        }

    def confirmed(self, **changes):
        candidate = self.freeze()
        self.study.record_evaluation(
            candidate["id"], self.report(candidate, "development")
        )
        self.study.nominate(candidate["id"])
        self.study.record_evaluation(candidate["id"], self.report(candidate, **changes))
        return candidate

    def publication(self):
        path = self.base / "publication.json"
        state = self.study.status()
        candidate = state["candidates"]["candidate"]
        path.write_text(
            json.dumps(
                {
                    "complete": True,
                    "iteration": "0001",
                    "supervisor_ref": candidate["parent_ref"],
                    "candidate_ref": candidate["ref"],
                    "protocol_ref": state["protocol_ref"],
                }
            )
        )
        return path

    def test_freeze_is_exact_and_source_mutation_does_not_follow(self):
        candidate = self.freeze()
        version = self.study.version(candidate["ref"])
        frozen = Path(version["path"]) / "src/research_loop/core.py"
        (self.source / "src/research_loop/core.py").write_text("VALUE = 'changed'\n")
        self.assertEqual(frozen.read_text(), "VALUE = 'baseline'\n")
        self.assertEqual(frozen.stat().st_mode & 0o222, 0)
        self.assertEqual(
            json.loads((Path(version["path"]) / "strategy.json").read_text()),
            candidate["policy"],
        )
        with self.assertRaises(MetaError):
            self.study.freeze_candidate(candidate["policy"], "0" * 40, "wrong-parent")
        with self.assertRaises(ValueError):
            self.study.freeze_candidate(
                {"selection": "direct", "review": "concise", "tools": "unrestricted"},
                candidate["parent_ref"],
                "scope",
            )

    def test_smoke_cannot_promote(self):
        self.study = self.initialize("development", "smoke")
        candidate = self.freeze()
        self.study.record_evaluation(
            candidate["id"], self.report(candidate, "development")
        )
        result = self.study.decide(candidate["id"])
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(self.study.status()["supervisor_ref"], candidate["parent_ref"])

    def test_total_time_and_tokens_each_block_overhead_win(self):
        for changes, category in (
            ({"total_seconds": 100}, "total_time"),
            ({"tokens": 110}, "tokens"),
        ):
            with self.subTest(category=category):
                self.study = self.initialize(name=category)
                candidate = self.confirmed(**changes)
                result = self.study.decide(candidate["id"])
                self.assertEqual(result["outcome"], "rejected")
                self.assertIn(category, result["failure_categories"])
                self.assertEqual(
                    self.study.status()["supervisor_ref"], candidate["parent_ref"]
                )

    def test_quality_and_canary_block_promotion(self):
        candidate = self.confirmed(quality=1.02)
        decision = self.study.decide(candidate["id"])
        self.assertIn("quality_S", decision["failure_categories"])
        self.study = self.initialize(name="canary")
        candidate = self.freeze()
        self.study.record_evaluation(
            candidate["id"], self.report(candidate, "development")
        )
        self.study.nominate(candidate["id"])
        report = self.report(candidate)
        report["canary"]["C"] = False
        self.study.record_evaluation(candidate["id"], report)
        self.assertIn(
            "canary", self.study.decide(candidate["id"])["failure_categories"]
        )

    def test_a0_quality_guard_is_separate_from_supervisor(self):
        # Construct a later-generation fixture whose S has already drifted 1%.
        old = self.study.status()["supervisor_ref"]
        supervisor = self.study._snapshot(
            Path(self.study.version(old)["path"]),
            {"selection": "direct", "review": "full"},
        )
        with self.study._locked() as state:
            state["supervisor_ref"] = supervisor["ref"]
            self.study._save(state)
        candidate = self.freeze()
        self.study.record_evaluation(
            candidate["id"], self.report(candidate, "development")
        )
        self.study.nominate(candidate["id"])
        report = self.report(candidate, quality=1.015)
        for row in report["rows"]:
            if row["arm"] == "S":
                row["quality"] = 1.01
        self.study.record_evaluation(candidate["id"], report)
        decision = self.study.decide(candidate["id"])
        self.assertIn("quality_A0", decision["failure_categories"])
        self.assertNotIn("quality_S", decision["failure_categories"])

    def test_missing_usage_and_cherry_dropped_rows_are_not_evidence(self):
        candidate = self.freeze()
        report = self.report(candidate, "development")
        for mutate in (
            lambda r: r["rows"][0].pop("tokens"),
            lambda r: r["rows"].pop(),
            lambda r: r["rows"][0].update(tokens=float("nan")),
        ):
            broken = copy.deepcopy(report)
            mutate(broken)
            with self.assertRaises(ValueError):
                self.study.record_evaluation(candidate["id"], broken)
        self.assertEqual(self.study.decide(candidate["id"])["outcome"], "inconclusive")

    def test_confirmation_requires_nomination_and_receipt_binding(self):
        candidate = self.freeze()
        with self.assertRaises(MetaError):
            self.study.record_evaluation(candidate["id"], self.report(candidate))
        report = self.report(candidate, "development")
        report["rows"][0]["total_seconds"] += 1
        with self.assertRaises(MetaError):
            self.study.record_evaluation(candidate["id"], report)

    def test_budget_reservations_persist_and_unknown_jobs_do_not_relaunch(self):
        self.study = self.initialize(name="budget", budget={"calls": 3, "runs": 2})
        ref = self.study.status()["supervisor_ref"]
        self.study.reserve_job("first", ref, 2, 100, 1000)
        resumed = MetaStudy(self.study.root)
        with self.assertRaises(MetaError):
            resumed.reserve_job("first", ref, 2, 100, 1000)
        resumed.finish_job(
            "first", {"status": "unknown", "calls": 0, "seconds": 0, "tokens": 0}
        )
        with self.assertRaises(MetaError):
            resumed.reserve_job("second", ref, 2, 100, 1000)
        resumed.finish_job(
            "first", {"status": "completed", "calls": 1, "seconds": 30, "tokens": 100}
        )
        resumed.reserve_job("second", ref, 2, 100, 1000)
        self.assertEqual(len(resumed.status()["jobs"]), 2)

    def test_formal_protocol_cannot_weaken_checks_or_claim_missing_isolation(self):
        for change in (
            {"checks": ["control"]},
            {"conditions": {"model": "fixed", "read_isolation": "unverified"}},
        ):
            invalid = protocol() | change
            with self.assertRaises(MetaError):
                MetaStudy.initialize(
                    self.base / "invalid",
                    self.source,
                    invalid,
                    {"selection": "debate", "review": "full"},
                )

    def test_slot_relaunch_and_scope_relabel_are_rejected(self):
        candidate = self.freeze()
        report = self.report(candidate, "development")
        row = report["rows"][0]
        evaluation = {
            "candidate_id": candidate["id"],
            "stage": "development",
            "arm": "S",
            "task": row["task"],
            "repeat": 0,
        }
        with self.assertRaises(MetaError):
            self.study.reserve_job(
                "replacement", row["version_ref"], 2, 200, 1000, evaluation=evaluation
            )
        report["rows"][0]["repeat"] = False
        with self.assertRaises(MetaError):
            self.study.record_evaluation(candidate["id"], report)

    def test_budget_breach_keeps_actual_usage_and_stops_new_work(self):
        ref = self.study.status()["supervisor_ref"]
        self.study.reserve_job("overrun", ref, 1, 100, 1000)
        self.study.finish_job(
            "overrun", {"status": "completed", "calls": 2, "seconds": 90, "tokens": 100}
        )
        state = self.study.status()
        self.assertEqual(state["jobs"]["overrun"]["receipt"]["calls"], 2)
        self.assertEqual(state["halted"]["reason"], "reservation_exceeded")
        with self.assertRaises(MetaError):
            self.study.reserve_job("next", ref, 1, 100, 1000)

    def test_success_requires_publication_and_replay_rollback_preserve_history(self):
        candidate = self.confirmed()
        with self.assertRaises(MetaError):
            self.study.decide(candidate["id"])
        result = self.study.decide(candidate["id"], publication=self.publication())
        self.assertEqual(result["outcome"], "promoted")
        self.assertEqual(self.study.status()["supervisor_ref"], candidate["ref"])
        self.assertEqual(self.study.decide(candidate["id"]), result)
        with self.assertRaises(MetaError):
            self.study.rollback("contract broke", candidate["parent_ref"])
        rolled = self.study.rollback(
            "reproducible recovery contract failure", candidate["ref"]
        )
        self.assertEqual(rolled["new_ref"], candidate["parent_ref"])
        self.assertEqual(len(self.study.status()["history"]), 2)
        self.assertTrue(Path(self.study.version(candidate["ref"])["path"]).is_dir())
        public = self.study.root / "decisions" / f"{result['id']}.json"
        self.assertNotIn("rows", json.loads(public.read_text()))
        with self.assertRaises(MetaError):
            self.freeze(name="after-feedback")

    def test_promotion_intent_recovers_only_exact_old_new_pair(self):
        candidate = self.confirmed()
        decision = self.study.decide(candidate["id"], publication=self.publication())
        state_path = self.study.controller / "state.json"
        state = json.loads(state_path.read_text())
        state["supervisor_ref"] = candidate["parent_ref"]
        state["history"] = []
        state["candidates"][candidate["id"]]["status"] = "confirmed"
        state_path.write_text(json.dumps(state))
        pending = self.study.controller / "pending.json"
        pending.write_text(json.dumps(decision))
        self.assertEqual(
            MetaStudy(self.study.root).status()["supervisor_ref"], candidate["ref"]
        )
        self.assertFalse(pending.exists())
        pending.write_text(json.dumps(decision))
        state = json.loads(state_path.read_text())
        state["supervisor_ref"] = "f" * 40
        state_path.write_text(json.dumps(state))
        with self.assertRaises(MetaError):
            self.study.status()


if __name__ == "__main__":
    unittest.main()
