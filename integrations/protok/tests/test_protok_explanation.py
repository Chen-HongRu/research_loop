"""Offline tests of presentation isolation and accepted evidence boundaries."""

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from integrations.protok import explanation as ex
from integrations.protok.backend import CANDIDATE_FILES, METRIC


class ExplanationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.prefix = "study/iterations/0001/"
        self.put(
            self.prefix + "iteration.json",
            {"id": "0001", "status": "reviewed", "base_experiment": None},
        )
        self.put(
            self.prefix + "context/study.before.json",
            {
                "global_evaluation": {
                    "id": "eval",
                    "comparison_scope": "dev",
                    "data_role": "development",
                }
            },
        )
        self.put(self.prefix + "context/goal.md", "研究目标")
        self.put(self.prefix + "context/route.before.md", "路线")
        saved = {}
        for phase, key in (
            ("plan", "plan_ref"),
            ("execute", "execution_ref"),
            ("review", "review_ref"),
        ):
            path = f"iterations/0001/{phase}/attempt-001/response.json"
            self.put("study/" + path, {"markdown": "accepted " + phase})
            self.put(
                str(Path("study/" + path).parent / "process.json"),
                {"status": "succeeded"},
            )
            saved[key] = path
        self.put(self.prefix + "publication.json", {"state_after": saved})
        self.put(
            self.prefix + "candidates.json",
            {
                "candidates": [
                    {
                        "id": "c1",
                        "experiment_id": "0001",
                        "valid": True,
                        "verified": True,
                        "evaluation_id": "eval",
                        "comparison_scope": "dev",
                        "metrics": {METRIC: 2.0},
                    }
                ]
            },
        )
        self.put(
            "control/jobs/0001/result.json",
            {"provenance": "historical-import-no-allocation"},
        )
        for name in CANDIDATE_FILES:
            self.put(
                "control/jobs/0001/source/" + name,
                "loss = prediction.square().mean()\n",
            )

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def content(self):
        statement = {
            "text": "记录支持本轮的实际修改。",
            "source_ids": [
                next(
                    s["id"]
                    for s in ex.collect_material(self.root, "0001")["sources"]
                    if s["kind"] == "review_ref"
                )
            ],
        }
        return {
            "title": "本轮研究说明",
            **{
                k: copy.deepcopy(statement)
                for k in ("overview", "question", "goal_connection", "change")
            },
            **{
                k: []
                for k in (
                    "observations",
                    "supported",
                    "hypotheses",
                    "unmeasured",
                    "next_tests",
                    "glossary",
                    "missing_material",
                )
            },
            "technical": {
                "kind": "loss",
                "before": statement,
                "after": statement,
                "formulas": [],
                "flow": [],
                "unchanged": [],
                "missing": ["没有冻结的对照源码时不提供公式"],
            },
        }

    def test_exact_refs_ignore_later_attempt_and_partial(self):
        self.put(
            self.prefix + "review/attempt-002/response.json", {"markdown": "later"}
        )
        material = ex.collect_material(self.root, "0001")
        texts = " ".join(s["text"] for s in material["sources"])
        self.assertIn("accepted review", texts)
        self.assertNotIn("later", texts)
        (self.root / (self.prefix + "execute/attempt-001/process.json")).unlink()
        material = ex.collect_material(self.root, "0001")
        self.assertNotIn("execution_ref", {s["kind"] for s in material["sources"]})

    def test_numeric_authority_import_and_incompatible(self):
        material = ex.collect_material(self.root, "0001")
        self.assertEqual(
            material["facts"]["measurements"], [{"candidate_id": "c1", "value": 2.0}]
        )
        self.assertTrue(material["facts"]["imported"])
        self.put(
            self.prefix + "context/study.before.json",
            {"global_evaluation": {"id": "different"}},
        )
        self.assertEqual(
            ex.collect_material(self.root, "0001")["facts"]["measurements"], []
        )
        bad = self.content()
        bad["value"] = 0.1
        with self.assertRaises(ValueError):
            ex.validate_content(bad, material)

    def test_invalid_citation_and_missing_code_formula(self):
        material = ex.collect_material(self.root, "0001")
        bad = self.content()
        bad["change"]["source_ids"] = ["invented"]
        with self.assertRaisesRegex(ValueError, "citation"):
            ex.validate_content(bad, material)
        bad = self.content()
        bad["technical"]["formulas"] = [
            {
                "before_latex": "L=x",
                "after_latex": "L=x^2",
                "scope": "loss",
                "variables": [],
                "source_ids": ["s1"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "frozen code"):
            ex.validate_content(bad, material)

    def test_cache_read_no_model_identity_invalidation_and_no_repeat(self):
        harness = Mock()
        harness.run.return_value = self.content()
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(
            ex.generate_one(self.root, "0001", harness=harness)["status"], "ready"
        )
        self.assertEqual(ex.explanation_status(self.root, "0001")["status"], "ready")
        ex.generate_one(self.root, "0001", harness=harness)
        self.assertEqual(harness.run.call_count, 1)
        self.assertFalse(harness.run.call_args.kwargs["writable"])
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value)
        self.put(self.prefix + "context/goal.md", "changed goal")
        self.assertEqual(ex.explanation_status(self.root, "0001")["status"], "stale")
        harness.run.side_effect = RuntimeError("offline")
        self.assertEqual(
            ex.generate_one(self.root, "0001", harness=harness)["status"], "failed"
        )
        ex.generate_one(self.root, "0001", harness=harness)
        self.assertEqual(harness.run.call_count, 2)
        ex.generate_one(self.root, "0001", harness=harness, retry_failed=True)
        self.assertEqual(harness.run.call_count, 3)

    def test_publication_only_and_bounds(self):
        self.put(
            "study/iterations/0002/iteration.json", {"id": "0002", "status": "executed"}
        )
        self.put("study/iterations/0002/publication.json", {})
        harness = Mock()
        harness.run.return_value = self.content()
        index = ex.generate_pending(self.root, harness=harness)
        self.assertEqual([e["id"] for e in index["entries"]], ["0001"])
        self.assertEqual(
            ex.explanation_status(self.root, "0002")["status"], "unpublished"
        )
        self.assertEqual(ex.explanation_coverage(self.root), index)
        self.put(
            "control/jobs/0001/source/" + CANDIDATE_FILES[0],
            "x" * (ex.MAX_FILE_BYTES + 1),
        )
        outside = self.root.parent / (self.root.name + "-outside")
        outside.write_text("secret")
        self.addCleanup(outside.unlink)
        path = self.root / "control/jobs/0001/source" / CANDIDATE_FILES[1]
        path.unlink()
        path.symlink_to(outside)
        material = ex.collect_material(self.root, "0001")
        self.assertLessEqual(
            sum(len(s["text"].encode()) for s in material["sources"]),
            ex.MAX_TOTAL_BYTES,
        )
        self.assertNotIn("secret", " ".join(s["text"] for s in material["sources"]))
        self.assertFalse(
            any(s["path"].endswith(CANDIDATE_FILES[0]) for s in material["sources"])
        )

    def test_declared_base_and_negative_result_are_preserved(self):
        self.put(
            self.prefix + "iteration.json",
            {"id": "0001", "status": "reviewed", "base_experiment": "0007"},
        )
        base = "study/iterations/0007/"
        self.put(base + "iteration.json", {"id": "0007", "status": "reviewed"})
        self.put(base + "publication.json", {"state_after": {}})
        self.put(
            base + "candidates.json",
            {
                "candidates": [
                    {
                        "id": "base7",
                        "experiment_id": "0007",
                        "valid": True,
                        "verified": True,
                        "evaluation_id": "eval",
                        "comparison_scope": "dev",
                        "metrics": {METRIC: 1.0},
                    }
                ]
            },
        )
        for name in CANDIDATE_FILES:
            self.put(
                "control/jobs/0007/source/" + name, "loss = prediction.abs().mean()\n"
            )
        material = ex.collect_material(self.root, "0001")
        self.assertEqual(material["facts"]["base_experiment"], "0007")
        self.assertEqual(
            material["facts"]["base_measurements"],
            [{"candidate_id": "base7", "value": 1.0}],
        )
        self.assertGreater(
            material["facts"]["measurements"][0]["value"],
            material["facts"]["base_measurements"][0]["value"],
        )
        self.assertTrue(any(s["kind"] == "source_diff" for s in material["sources"]))

    def test_missing_sources_cannot_support_formula(self):
        for name in CANDIDATE_FILES:
            (self.root / "control/jobs/0001/source" / name).unlink()
        material = ex.collect_material(self.root, "0001")
        self.assertFalse(any(s["kind"] == "actual_source" for s in material["sources"]))
        content = self.content()
        content["technical"]["formulas"] = [
            {
                "before_latex": "L=x",
                "after_latex": "L=x^2",
                "scope": "loss",
                "variables": [],
                "source_ids": ["s1"],
            }
        ]
        with self.assertRaisesRegex(ValueError, "frozen code"):
            ex.validate_content(content, material)

    def test_cached_reads_and_watch_do_not_collect_source_again(self):
        harness = Mock()
        harness.run.return_value = self.content()
        self.assertEqual(
            ex.generate_one(self.root, "0001", harness=harness)["status"], "ready"
        )
        with patch.object(
            ex, "collect_material", side_effect=AssertionError("source reread")
        ):
            self.assertEqual(
                ex.explanation_status(self.root, "0001")["status"], "ready"
            )
            self.assertEqual(
                ex.generate_pending(self.root, harness=harness)["counts"]["ready"], 1
            )
        self.assertEqual(harness.run.call_count, 1)

    def test_index_published_before_during_and_after_generation(self):
        content = self.content()
        harness = Mock()

        def run(**kwargs):
            index = ex.explanation_coverage(self.root)
            self.assertEqual(index["counts"]["running"], 1)
            self.assertEqual(
                ex.explanation_status(self.root, "0001")["status"], "running"
            )
            return content

        harness.run.side_effect = run
        initial = ex._refresh_index(self.root)
        self.assertEqual(initial["counts"]["pending"], 1)
        ex.generate_one(self.root, "0001", harness=harness)
        index = ex.explanation_coverage(self.root)
        self.assertEqual(index["covered_through_id"], "0001")
        self.assertEqual(index["entries"][0]["title"], content["title"])
        self.assertEqual(index["entries"][0]["question"], content["question"]["text"])

    def test_interrupted_writer_is_failed_without_model_retry(self):
        harness = Mock()
        harness.run.return_value = self.content()
        ready = ex.generate_one(self.root, "0001", harness=harness)
        ready.pop("content")
        ready["status"] = "running"
        ex._write(ex._cache(self.root, "0001"), ready)
        self.assertEqual(ex.explanation_status(self.root, "0001")["status"], "failed")
        result = ex.generate_one(self.root, "0001", harness=harness)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["interrupted"])
        self.assertEqual(harness.run.call_count, 1)

    def test_missing_review_is_insufficient_without_model(self):
        (self.root / (self.prefix + "review/attempt-001/process.json")).unlink()
        harness = Mock()
        result = ex.generate_one(self.root, "0001", harness=harness)
        self.assertEqual(result["status"], "insufficient")
        harness.run.assert_not_called()

    def test_metric_prose_and_oversized_content_rejected(self):
        material = ex.collect_material(self.root, "0001")
        for text in (
            "RMSD从2.1到1.9",
            "误差为1.88 Å",
            "提升12百分比",
            "重建误差在449个样本评估",
        ):
            bad = self.content()
            bad["overview"]["text"] = text
            with self.assertRaisesRegex(ValueError, "Metric figures"):
                ex.validate_content(bad, material)
        bad = self.content()
        bad["observations"] = [
            {"text": "中" * 1800, "source_ids": ["s1"]} for _ in range(12)
        ]
        with self.assertRaisesRegex(ValueError, "byte limit"):
            ex.validate_content(bad, material)

    def test_flow_plan_only_and_formula_one_sided_rejected(self):
        material = ex.collect_material(self.root, "0001")
        plan = next(s["id"] for s in material["sources"] if s["kind"] == "plan_ref")
        code = next(
            s["id"]
            for s in material["sources"]
            if s["kind"] == "actual_source" and s["path"].endswith(".py")
        )
        bad = self.content()
        bad["change"]["source_ids"] = [plan]
        with self.assertRaisesRegex(ValueError, "Actual change"):
            ex.validate_content(bad, material)
        bad = self.content()
        bad["technical"]["flow"] = [
            {"label": "flow", "shape": "[B,L,16]", "source_ids": [plan]}
        ]
        with self.assertRaisesRegex(ValueError, "Flow"):
            ex.validate_content(bad, material)
        bad = self.content()
        bad["technical"]["formulas"] = [
            {
                "before_latex": "L=x",
                "after_latex": "L=x^2",
                "scope": "loss",
                "variables": [],
                "source_ids": [code],
            }
        ]
        with self.assertRaisesRegex(ValueError, "base frozen code"):
            ex.validate_content(bad, material)

    def test_base_wrong_scope_never_supplies_comparison_number(self):
        self.test_declared_base_and_negative_result_are_preserved()
        path = self.root / "study/iterations/0007/candidates.json"
        record = json.loads(path.read_text())
        record["candidates"][0]["comparison_scope"] = "other"
        self.put("study/iterations/0007/candidates.json", record)
        self.assertEqual(
            ex.collect_material(self.root, "0001")["facts"]["base_measurements"], []
        )

    def test_existing_codex_executable_is_reused(self):
        self.put(
            "study/study.json",
            {"harness": {"executable": "/installed/bin/codex", "model": "gpt-6-astra"}},
        )
        writer = Mock()
        writer.run.return_value = self.content()
        with patch.object(ex, "CodexHarness", return_value=writer) as constructor:
            self.assertEqual(ex.generate_one(self.root, "0001")["status"], "ready")
        config = constructor.call_args.args[0]
        self.assertEqual(config.executable, "/installed/bin/codex")
        self.assertEqual((config.model, config.reasoning), ("gpt-6.1-sol", "medium"))

    def test_real_harness_has_run_directory_before_subprocess(self):
        content = self.content()

        def execute(argv, *, run_dir, **kwargs):
            self.assertTrue(run_dir.is_dir())
            self.assertTrue((run_dir / "prompt.md").is_file())
            self.assertTrue((run_dir / "schema.json").is_file())
            (run_dir / "response.json").write_text(json.dumps(content))

        with patch("research_loop.harness._execute", side_effect=execute) as process:
            result = ex.generate_one(self.root, "0001")
        self.assertEqual(result["status"], "ready")
        process.assert_called_once()

    def test_cli_once_prints_status_and_failure_exit_code(self):
        for status, expected in (("ready", 0), ("failed", 1), ("insufficient", 1)):
            output = io.StringIO()
            with (
                patch.object(
                    ex,
                    "generate_one",
                    return_value={"status": status, "reason": "example"},
                ),
                contextlib.redirect_stdout(output),
            ):
                code = ex.main([str(self.root), "--once", "--iteration", "0001"])
            self.assertEqual(code, expected)
            self.assertEqual(
                json.loads(output.getvalue()),
                {"iteration": "0001", "status": status, "reason": "example"},
            )

    def test_coverage_order_ignores_copied_publication_mtime(self):
        self.put(
            self.prefix + "iteration.json",
            {"id": "0001", "status": "reviewed", "created_at": "2026-10-01T00:00:00Z"},
        )
        self.put(
            "study/iterations/0002/iteration.json",
            {"id": "0002", "status": "reviewed", "created_at": "2026-10-02T00:00:00Z"},
        )
        second = self.put(
            "study/iterations/0002/publication.json",
            {"published_at": "2026-10-02T01:00:00Z"},
        )
        os.utime(
            self.root / (self.prefix + "publication.json"),
            (2_000_000_000, 2_000_000_000),
        )
        os.utime(second, (1_000_000_000, 1_000_000_000))
        index = ex._refresh_index(self.root)
        self.assertEqual(index["latest_published_id"], "0002")
        self.assertEqual(index["latest_published"], "2026-10-02T01:00:00Z")

    def test_large_publication_metadata_uses_only_publication_allowance(self):
        path = self.root / (self.prefix + "publication.json")
        publication = json.loads(path.read_text())
        publication["frozen_writes"] = "x" * 130_000
        self.put(self.prefix + "publication.json", publication)
        self.assertIsNone(ex._small(self.root, path))
        material = ex.collect_material(self.root, "0001")
        self.assertTrue(material["published"])
        self.assertTrue(material["publication_available"])
        self.assertTrue(any(s["kind"] == "review_ref" for s in material["sources"]))
        self.assertEqual(ex._refresh_index(self.root)["latest_published_id"], "0001")

    def test_oversized_and_malformed_publications_remain_insufficient_entries(self):
        harness = Mock()
        for value in ("x" * (ex.MAX_PUBLICATION_BYTES + 1), "{broken"):
            self.put(self.prefix + "publication.json", value)
            material = ex.collect_material(self.root, "0001")
            self.assertTrue(material["published"])
            self.assertFalse(material["publication_available"])
            self.assertEqual(
                ex.explanation_status(self.root, "0001")["status"], "insufficient"
            )
            index = ex.generate_pending(self.root, harness=harness)
            self.assertEqual(index["counts"]["insufficient"], 1)
            self.assertEqual(index["entries"][0]["id"], "0001")
            self.assertIn("256KB", index["entries"][0]["reason"])
            self.assertEqual(
                json.loads(ex._cache(self.root, "0001").read_text())["status"],
                "insufficient",
            )
        harness.run.assert_not_called()

    def test_invalid_recovery_uses_workspace_and_repair_invalidates_identity(self):
        self.put(
            "control/jobs/0001/recovery.json",
            {"job_directory": "/outside/not-authorized"},
        )
        for name in CANDIDATE_FILES:
            self.put(
                self.prefix + "workspace/" + name, "# frozen published workspace\n"
            )
        material = ex.collect_material(self.root, "0001")
        actual = [s for s in material["sources"] if s["kind"] == "actual_source"]
        self.assertTrue(actual)
        self.assertTrue(all("/workspace/" in s["path"] for s in actual))
        self.assertTrue(
            any(
                "recovery pointer rejected" in reason
                for reason in material["missing_material"]
            )
        )
        harness = Mock()
        harness.run.return_value = self.content()
        self.assertEqual(
            ex.generate_one(self.root, "0001", harness=harness)["status"], "ready"
        )
        self.put(
            "control/jobs/0001/recovery.json",
            {"job_directory": str(self.root / "control/debug/0001/formal-001")},
        )
        self.assertEqual(ex.explanation_status(self.root, "0001")["status"], "stale")
        repaired = ex.collect_material(self.root, "0001")
        self.assertNotEqual(repaired["source_identity"], material["source_identity"])

    def test_explicit_executable_override_preserves_frozen_config_and_is_recorded(self):
        configured = "/Applications/ChatGPT.app/Contents/MacOS/codex"
        effective = "/usr/local/bin/codex"
        config_path = self.put(
            "study/study.json", {"harness": {"executable": configured}}
        )
        frozen = config_path.read_bytes()
        content = self.content()

        def execute(argv, *, run_dir, **kwargs):
            self.assertEqual(argv[0], effective)
            (run_dir / "response.json").write_text(json.dumps(content))

        with patch("research_loop.harness._execute", side_effect=execute):
            index = ex.generate_pending(self.root, codex_executable=effective)
        self.assertEqual(index["counts"]["ready"], 1)
        self.assertEqual(config_path.read_bytes(), frozen)
        cached = json.loads(ex._cache(self.root, "0001").read_text())
        process = json.loads(
            next(
                (self.root / "presentation/staging/0001").glob("*/run/process.json")
            ).read_text()
        )
        for evidence in (cached, process):
            self.assertEqual(evidence["configured_codex_executable"], configured)
            self.assertEqual(evidence["effective_codex_executable"], effective)
            self.assertEqual(evidence["codex_executable_override"], effective)

    def test_cli_executable_override_is_explicitly_propagated(self):
        with (
            patch.object(
                ex, "generate_one", return_value={"status": "ready"}
            ) as generate,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                ex.main(
                    [
                        str(self.root),
                        "--once",
                        "--iteration",
                        "0001",
                        "--codex-executable",
                        "/usr/local/bin/codex",
                    ]
                ),
                0,
            )
        self.assertEqual(
            generate.call_args.kwargs["codex_executable"], "/usr/local/bin/codex"
        )


if __name__ == "__main__":
    unittest.main()
