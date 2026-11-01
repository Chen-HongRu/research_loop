"""Offline tests of presentation isolation and accepted evidence boundaries."""

import copy
import contextlib
import hashlib
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

    def test_ordinary_material_preserves_historical_cache_identity(self):
        with patch.object(ex, "execution_plan") as resolver:
            material = ex.collect_material(self.root, "0001")
            resolver.assert_not_called()
        paths = [
            self.prefix + "iteration.json",
            self.prefix + "publication.json",
            self.prefix + "context/goal.md",
            self.prefix + "context/study.before.json",
            self.prefix + "context/route.before.md",
        ]
        for phase in ("plan", "execute", "review"):
            folder = self.prefix + phase + "/attempt-001/"
            paths.extend(
                folder + name
                for name in (
                    "response.json",
                    "completed.json",
                    "process.json",
                    "response.json",
                )
            )
        paths.extend(
            (
                self.prefix + "candidates.json",
                "control/jobs/0001/recovery.json",
                "control/jobs/0001/result.json",
            )
        )
        paths.extend("control/jobs/0001/source/" + name for name in CANDIDATE_FILES)
        paths.append("study/study.json")
        root = self.root.resolve()
        dependencies = [ex._stamp(root, root / path) for path in paths]
        expected = hashlib.sha256(
            json.dumps(
                [
                    "research-explanation-2",
                    "gpt-6.1-sol",
                    "medium",
                    240,
                    dependencies,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.assertEqual(material["dependencies"], dependencies)
        self.assertEqual(material["source_identity"], expected)
        for key in ("plan_iteration", "execution_iteration", "continuation"):
            self.assertNotIn(key, material["facts"])

    def continuation(self, *, legacy=False):
        original = {
            "decision": "investigate",
            "contract": {"question": "original hypothesis"},
        }
        self.put(self.prefix + "plan/attempt-001/response.json", original)
        source_meta = json.loads(
            (self.root / (self.prefix + "iteration.json")).read_text()
        )
        source_meta["workspace_ref"] = "iterations/0001/workspace"
        self.put(self.prefix + "iteration.json", source_meta)
        source_publication = json.loads(
            (self.root / (self.prefix + "publication.json")).read_text()
        )
        source_publication["metadata"] = source_meta
        state = source_publication["state_after"]
        state["calls"] = {"plan-001": "iterations/0001/plan/attempt-001"}
        state["accepted_calls"] = {"plan-001": True}
        self.put(self.prefix + "publication.json", source_publication)
        marker = {
            "source_iteration": "0001",
            "target_iteration": "0002",
            "source_workspace_ref": source_meta["workspace_ref"],
        }
        target = "study/iterations/0002/"
        target_state = {}
        if legacy:
            target_state["plan_ref"] = "iterations/0002/operator-plan/response.json"
            target_state["selected"] = {"source": "operator"}
            self.put(
                target + "operator-plan/operator.json",
                {
                    **marker,
                    "source": "operator",
                    "authorization": "resume authorized",
                },
            )
            self.put(
                target + "operator-plan/response.json",
                {
                    "decision": "investigate",
                    "base_experiment": "0001",
                    "contract": {},
                    "markdown": "continue execution",
                },
            )
        else:
            marker.update(
                plan_ref=state["plan_ref"],
                authorization="resume authorized",
                directive="continue execution",
            )
            target_state["plan_ref"] = state["plan_ref"]
        metadata = {
            "id": "0002",
            "status": "reviewed",
            "base_experiment": None,
            "operator_resume" if legacy else "continuation": marker,
        }
        self.put(target + "iteration.json", metadata)
        for phase, key in (("execute", "execution_ref"), ("review", "review_ref")):
            ref = f"iterations/0002/{phase}/attempt-001/response.json"
            self.put("study/" + ref, {"markdown": "resumed " + phase})
            self.put(
                str(Path("study/" + ref).parent / "process.json"),
                {"status": "succeeded"},
            )
            target_state[key] = ref
        self.put(
            target + "publication.json",
            {"metadata": metadata, "state_after": target_state},
        )
        return original

    def test_inherited_plan_new_and_legacy_are_original_completed_evidence(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                original = self.continuation(legacy=legacy)
                with patch.object(ex, "CodexHarness") as harness:
                    material = ex.collect_material(self.root, "0002")
                    harness.assert_not_called()
                plans = [s for s in material["sources"] if s["kind"] == "plan_ref"]
                self.assertEqual(len(plans), 1)
                self.assertEqual(
                    plans[0]["path"], self.prefix + "plan/attempt-001/response.json"
                )
                self.assertEqual(json.loads(plans[0]["text"]), original)
                self.assertEqual(material["facts"]["plan_iteration"], "0001")
                self.assertEqual(material["facts"]["execution_iteration"], "0002")
                self.assertEqual(material["facts"]["continuation"]["legacy"], legacy)
                directives = [
                    s for s in material["sources"] if s["kind"] == "execution_directive"
                ]
                self.assertEqual(
                    json.loads(directives[0]["text"])["directive"], "continue execution"
                )
                self.assertIn(
                    "continuation_evidence", {s["kind"] for s in material["sources"]}
                )
                self.assertNotIn("plan_ref", material["missing_material"])

    def test_inherited_plan_requires_matching_accepted_and_completed_reference(self):
        for invalid in ("mismatch", "unaccepted", "incomplete"):
            with self.subTest(invalid=invalid):
                self.continuation()
                if invalid == "mismatch":
                    path = "study/iterations/0002/publication.json"
                    value = json.loads((self.root / path).read_text())
                    value["state_after"]["plan_ref"] = (
                        "iterations/0001/plan/attempt-002/response.json"
                    )
                elif invalid == "unaccepted":
                    path = self.prefix + "publication.json"
                    value = json.loads((self.root / path).read_text())
                    value["state_after"]["accepted_calls"]["plan-001"] = False
                else:
                    path = self.prefix + "plan/attempt-001/process.json"
                    value = {"status": "interrupted"}
                self.put(path, value)
                material = ex.collect_material(self.root, "0002")
                self.assertNotIn("plan_ref", {s["kind"] for s in material["sources"]})
                self.assertIsNone(material["facts"]["continuation"])
                self.assertIn("plan_ref", material["missing_material"])

    def test_inherited_plan_receipt_retains_small_file_bound(self):
        original = self.continuation()
        original["markdown"] = "x" * ex.MAX_FILE_BYTES
        self.put(self.prefix + "plan/attempt-001/response.json", original)
        material = ex.collect_material(self.root, "0002")
        self.assertNotIn("plan_ref", {s["kind"] for s in material["sources"]})
        self.assertIn("plan_ref", material["missing_material"])
        self.assertLessEqual(
            sum(len(s["text"].encode()) for s in material["sources"]),
            ex.MAX_TOTAL_BYTES,
        )

    def test_repaired_original_acceptance_invalidates_rejected_continuation_cache(self):
        self.continuation()
        path = self.root / (self.prefix + "publication.json")
        publication = json.loads(path.read_text())
        publication["state_after"]["accepted_calls"]["plan-001"] = False
        path.write_text(json.dumps(publication))
        material = ex.collect_material(self.root, "0002")
        self.assertIsNone(material["facts"]["continuation"])
        ex._write(
            ex._cache(self.root, "0002"),
            {
                "status": "ready",
                "source_identity": material["source_identity"],
                "dependencies": material["dependencies"],
            },
        )
        self.assertEqual(
            ex._cached_state(self.root.resolve(), "0002")["status"], "ready"
        )
        publication["state_after"]["accepted_calls"]["plan-001"] = True
        path.write_text(json.dumps(publication))
        self.assertEqual(
            ex._cached_state(self.root.resolve(), "0002")["status"], "stale"
        )
        repaired = ex.collect_material(self.root, "0002")
        self.assertEqual(repaired["facts"]["plan_iteration"], "0001")
        self.assertIn("plan_ref", {s["kind"] for s in repaired["sources"]})

    def test_old_legacy_cache_is_recollected_without_invalidating_ordinary_cache(self):
        harness = Mock()
        harness.run.return_value = self.content()
        ex.generate_one(self.root, "0001", harness=harness)
        self.assertEqual(ex.explanation_status(self.root, "0001")["status"], "ready")
        self.continuation(legacy=True)
        # The old collector saw the operator marker but used only local refs and
        # the ordinary v2 token; it had no inherited Plan proof dependencies.
        with patch.object(ex, "execution_plan", return_value=None):
            old_material = ex.collect_material(self.root, "0002")
        old_identity = ex._identity(old_material["dependencies"])
        old_material["source_identity"] = old_identity
        for key in ("plan_iteration", "execution_iteration", "continuation"):
            old_material["facts"].pop(key)
        ex._write(ex._material_path(self.root, "0002", old_identity), old_material)
        material = ex.collect_material(self.root, "0002")
        review_id = next(
            s["id"] for s in material["sources"] if s["kind"] == "review_ref"
        )
        content = self.content()
        for field in ("overview", "question", "goal_connection", "change"):
            content[field]["source_ids"] = [review_id]
        for field in ("before", "after"):
            content["technical"][field]["source_ids"] = [review_id]
        for status in ("ready", "insufficient"):
            with self.subTest(status=status):
                ex._write(
                    ex._cache(self.root, "0002"),
                    {
                        "status": status,
                        "source_identity": old_identity,
                        "dependencies": old_material["dependencies"],
                    },
                )
                self.assertEqual(
                    ex.explanation_status(self.root, "0002")["status"], "stale"
                )
                harness = Mock()
                harness.run.return_value = content
                with patch.object(
                    ex, "collect_material", wraps=ex.collect_material
                ) as collect:
                    result = ex.generate_one(self.root, "0002", harness=harness)
                    collect.assert_called_once_with(self.root.resolve(), "0002")
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["source_identity"], material["source_identity"])
                self.assertNotEqual(result["source_identity"], old_identity)
                harness.run.assert_called_once()

    def test_every_continuation_proof_dependency_invalidates_cached_material(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.continuation(legacy=legacy)
                material = ex.collect_material(self.root, "0002")
                proof = ex.execution_plan(self.root, "0002")
                dependencies = {
                    str((self.root / dep[0]).resolve().relative_to(self.root.resolve()))
                    for dep in material["dependencies"]
                }
                self.assertTrue(set(proof["evidence_paths"]) <= dependencies)
                for relative in proof["evidence_paths"]:
                    with self.subTest(path=relative):
                        ex._write(
                            ex._cache(self.root, "0002"),
                            {
                                "status": "ready",
                                "source_identity": material["source_identity"],
                                "dependencies": material["dependencies"],
                            },
                        )
                        self.assertEqual(
                            ex._cached_state(self.root.resolve(), "0002")["status"],
                            "ready",
                        )
                        path = self.root / relative
                        if path.exists():
                            stat = path.stat()
                            os.utime(
                                path,
                                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
                            )
                            self.assertEqual(
                                ex._cached_state(self.root.resolve(), "0002")["status"],
                                "stale",
                            )
                            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                        else:
                            self.put(relative, {})
                            self.assertEqual(
                                ex._cached_state(self.root.resolve(), "0002")["status"],
                                "stale",
                            )
                            path.unlink()

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

    def test_metric_guard_allows_only_known_experiment_identifiers(self):
        material = ex.collect_material(self.root, "0001")
        material["facts"]["base_experiment"] = "0007"
        content = self.content()
        content["question"]["text"] = (
            "能否降低目标重建误差？实际声明的 base_experiment 为 0007；本轮为0001。"
        )
        ex.validate_content(content, material)
        for text in (
            "RMSD 对照为9999",
            "RMSD从2到1",
            "RMSD为0.0001",
            "RMSD为10001",
            "0001 Å",
            "1.88 Å",
            "0007%",
        ):
            content["question"]["text"] = text
            with self.assertRaisesRegex(ValueError, "Metric figures"):
                ex.validate_content(content, material)

    def test_explicit_retry_reuses_valid_completed_response_without_model(self):
        content = self.content()

        def execute(argv, *, run_dir, **kwargs):
            (run_dir / "response.json").write_text(json.dumps(content))

        with patch("research_loop.harness._execute", side_effect=execute):
            ready = ex.generate_one(self.root, "0001")
        response = next(
            (self.root / "presentation/staging/0001").glob("*/run/response.json")
        )
        original = response.read_bytes()
        ready.pop("content")
        ready.update(status="failed", reason="previous validation false positive")
        ex._write(ex._cache(self.root, "0001"), ready)
        harness = Mock()
        recovered = ex.generate_one(
            self.root, "0001", retry_failed=True, harness=harness
        )
        self.assertEqual(recovered["status"], "ready")
        self.assertTrue(recovered["reused_completed_response"])
        harness.run.assert_not_called()
        self.assertEqual(response.read_bytes(), original)

    def test_invalid_completed_response_is_preserved_before_explicit_rerun(self):
        bad = self.content()
        bad["title"] = "RMSD从2到1"

        def execute(argv, *, run_dir, **kwargs):
            (run_dir / "response.json").write_text(json.dumps(bad))

        with patch("research_loop.harness._execute", side_effect=execute):
            self.assertEqual(ex.generate_one(self.root, "0001")["status"], "failed")
        harness = Mock()
        harness.run.return_value = self.content()
        self.assertEqual(
            ex.generate_one(self.root, "0001", retry_failed=True, harness=harness)[
                "status"
            ],
            "ready",
        )
        preserved = next(
            (self.root / "presentation/staging/0001").glob(
                "*/run.previous.*/response.json"
            )
        )
        self.assertEqual(json.loads(preserved.read_text()), bad)
        harness.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
