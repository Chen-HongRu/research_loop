"""Role declarations, frozen inputs, grants and previews without model calls."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from integrations.native_runtime import application, policy
from research_loop.runtime.call_context import read_at_call
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.role_access import RoleAccess
from research_loop.runtime.role_assembly import preview_assembly
from research_loop.runtime.store import StateStore


class RoleAssemblyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "transport": {
                "backend": "pi",
                "executable": "pi",
                "provider": "synthetic",
                "model": "synthetic",
                "reasoning": "medium",
                "timeout_seconds": 5,
            },
            "backend_handlers": {},
        }

    def payload(self, role="plan", extra=None, protected=None):
        inputs = dict(
            task=None,
            budget={},
            goal="synthetic",
            global_evaluation=None,
            permissions={},
        )
        inputs.update(extra or {})
        sources = [
            dict(
                id=key,
                key=key,
                ref="control:" + key,
                revision=1,
                reason="Synthetic controller input",
                allow_feedback=True,
            )
            for key in inputs
        ]
        snapshot = ContextSnapshot.capture(
            study_id="synthetic",
            revisions={},
            inputs=inputs,
            sources=sources,
            protected_keys=list(protected or []),
            allowed_evidence=[],
        )
        package = build_context(snapshot, role, None, "synthetic", {}).to_dict()
        return dict(
            call={"role": role, "attempt_id": "synthetic"},
            context_ref="artifacts/synthetic.json",
            package=package,
        )

    def prepare(self, payload=None, config=None):
        return application.prepare_agent(
            payload or self.payload(),
            self.config,
            study_config=config or {},
            study_root=self.root,
        )

    def test_all_seven_declarations_readable_and_schema_bound(self):
        application.validate_roles({})
        for role in policy.MODELS:
            required = {
                "execute": {"plan": {}},
                "review": {"plan": {}, "execute": {}, "measurement:m": {"id": "m"}},
            }.get(role, {})
            assembly = self.prepare(self.payload(role, required))
            self.assertEqual(
                set(assembly["definition"]),
                {
                    "role",
                    "instructions",
                    "inputs",
                    "knowledge_bases",
                    "tools",
                    "output",
                },
            )
            self.assertEqual(
                assembly["response_schema"],
                json.loads(assembly["prompt"].split("satisfy this JSON schema:\n")[-1]),
            )
            self.assertIn(assembly["instructions"], assembly["prompt"])
            self.assertEqual(assembly["knowledge_libraries"][0]["alias"], "study")

    def test_optional_input_selection_and_required_contract(self):
        payload = self.payload(extra={"provided:optional": {"secret": "omit me"}})
        config = {
            "role_definitions": {
                "plan": {
                    "inputs": [
                        "task",
                        "budget",
                        "goal",
                        "evaluation",
                        "permissions",
                        "operation_grants",
                    ]
                }
            }
        }
        assembly = self.prepare(payload, config)
        self.assertNotIn("provided:optional", assembly["inputs"])
        self.assertNotIn("omit me", assembly["prompt"])
        self.assertNotIn("omit me", json.dumps(preview_assembly(assembly, expand=True)))
        required = self.payload(
            extra={"provided:optional": {}}, protected=["provided:optional"]
        )
        with self.assertRaisesRegex(
            ValueError, "plan inputs: missing required sources"
        ):
            self.prepare(required, config)

    def test_unknown_and_unauthorized_configuration_fails_before_model(self):
        cases = [
            ({"inputs": ["unknown"]}, "unknown provider"),
            ({"tools": ["unknown", "submit_result"]}, "unknown registered tool"),
            ({"knowledge_bases": ["hidden"]}, "unknown library"),
            ({"output": "Unknown"}, "unknown type"),
        ]
        for override, message in cases:
            with (
                self.subTest(override=override),
                self.assertRaisesRegex(ValueError, message),
            ):
                self.prepare(config={"role_definitions": {"plan": override}})
        config = {
            "role_definitions": {"plan": {"tools": ["read", "submit_result"]}},
            "role_access": {"plan": {"tools": ["submit_result"]}},
        }
        with self.assertRaisesRegex(
            ValueError, "plan tools: exceeds Study authorization"
        ):
            self.prepare(config=config)
        config = {
            "role_definitions": {"plan": {"inputs": ["history"]}},
            "role_access": {"plan": {"published_history": False}},
        }
        with self.assertRaisesRegex(ValueError, "history exceeds Study authorization"):
            self.prepare(config=config)
        for role in ("execute", "review"):
            with self.assertRaisesRegex(ValueError, "missing required"):
                self.prepare(
                    self.payload(role), {"role_definitions": {role: {"inputs": []}}}
                )

    def test_config_only_library_and_tool_changes_preserve_old_assembly(self):
        mounted = KnowledgeBase(self.root / "mounted")
        manifest = mounted.freeze()
        config = {
            "knowledge_libraries": [
                {
                    "alias": "literature",
                    "path": str(self.root / "mounted"),
                    "version": manifest[0]["version"],
                    "description": "Public synthetic sources",
                }
            ]
        }
        old = self.prepare(config=config)
        config["role_definitions"] = {
            "plan": {
                "knowledge_bases": ["study", "literature"],
                "tools": ["read", "submit_result"],
            }
        }
        new = self.prepare(config=config)
        self.assertEqual(
            [entry["alias"] for entry in new["knowledge_libraries"]],
            ["study", "literature"],
        )
        self.assertNotIn("grep", new["tools"])
        self.assertEqual(
            [entry["alias"] for entry in old["knowledge_libraries"]], ["study"]
        )
        serialized = json.dumps(preview_assembly(old))
        self.assertNotIn("Public synthetic sources", serialized)
        self.assertNotIn(str(self.root), json.dumps(preview_assembly(new, expand=True)))

    def test_ordinary_grant_names_live_library_without_pinning_a_revision(self):
        assembly = self.prepare()
        self.assertEqual(assembly["knowledge_libraries"][0]["path"], "knowledge/study")
        self.assertNotIn("version", assembly["knowledge_libraries"][0])
        self.assertNotIn("maintenance_libraries", assembly)
        self.assertNotIn("history", assembly)
        self.assertNotIn("filesystem", preview_assembly(assembly, expand=True))

    def test_preview_and_real_transport_share_frozen_prompt_schema_and_tools(self):
        payload = self.payload()
        assembly = self.prepare(payload)
        payload.update(
            assembly=assembly,
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )
        response = {
            "output": {
                "timeout_seconds": 10,
                "local_evaluation": None,
                "experiment_definition": "Synthetic qualitative check",
            },
            "evidence_refs": [],
        }
        with patch(
            "integrations.native_runtime.transport.run", return_value=response
        ) as run:
            application.invoke("agent", payload, self.config)
        args, kwargs = run.call_args
        self.assertEqual(args[0], preview_assembly(assembly, expand=True)["prompt"])
        self.assertEqual(kwargs["response_schema"], assembly["response_schema"])
        self.assertEqual(
            set(kwargs["tools"]), set(assembly["tools"]) - {"submit_result"}
        )
        self.assertNotIn("prompt", preview_assembly(assembly))
        self.assertNotIn("package", preview_assembly(assembly))

    def test_native_default_history_is_retrieved_and_explicit_selection_preserved(self):
        config = {"runtime": {"worker_adapter": {"id": "native-runtime-v1"}}}
        self.assertFalse(RoleAccess.resolve(config, "plan").published_history)
        config["role_definitions"] = {"plan": {"inputs": ["history"]}}
        self.assertTrue(RoleAccess.resolve(config, "plan").published_history)
        config["role_access"] = {"plan": {"published_history": False}}
        self.assertFalse(RoleAccess.resolve(config, "plan").published_history)

    def test_explicit_citation_resolves_fixed_record_without_a_read_receipt(self):
        kb = KnowledgeBase(self.root)
        kb.publish_evidence("E1", "external", {"value": 3})
        payload = self.payload()
        payload.update(
            assembly=self.prepare(payload),
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )
        ref = f"kb:{kb.library_id}:evidence:E1@1"
        response = {
            "output": {
                "timeout_seconds": 10,
                "local_evaluation": None,
                "experiment_definition": "Synthetic reference.",
            },
            "evidence_refs": [ref],
        }
        with patch("integrations.native_runtime.transport.run", return_value=response):
            self.assertEqual(
                application.invoke("agent", payload, self.config)["evidence_refs"],
                [ref],
            )
        response["evidence_refs"] = [f"kb:{kb.library_id}:evidence:missing@1"]
        with (
            patch("integrations.native_runtime.transport.run", return_value=response),
            self.assertRaises((ValueError, PermissionError)),
        ):
            application.invoke("agent", payload, self.config)

    def test_omitted_original_evidence_is_not_a_citation_capability(self):
        payload = self.payload(extra={"optional": "hidden body"})
        original = payload["package"]
        next(
            source
            for source in original["manifest"]["sources"]
            if source["key"] == "optional"
        )["ref"] = "source:hidden"
        original["manifest"]["allowed_evidence"] = ["source:hidden"]
        assembly = self.prepare(payload)
        self.assertNotIn(
            "source:hidden", assembly["package"]["manifest"]["allowed_evidence"]
        )
        payload.update(
            assembly=assembly,
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )
        response = {
            "output": {
                "timeout_seconds": 10,
                "local_evaluation": None,
                "experiment_definition": "Synthetic",
            },
            "evidence_refs": ["source:hidden"],
        }
        with (
            patch("integrations.native_runtime.transport.run", return_value=response),
            self.assertRaisesRegex(ValueError, "outside the bound manifest"),
        ):
            application.invoke("agent", payload, self.config)

    def test_page_bytes_and_its_fixed_citation_are_independent(self):
        kb = KnowledgeBase(self.root)
        kb.commit(
            0,
            [
                dict(
                    id="k1",
                    title="Synthetic",
                    markdown="Synthetic knowledge",
                    dependencies=[],
                )
            ],
            "# Index",
            0,
            "citation",
        )
        directory = Path(self.prepare()["filesystem"]["mounts"]["study"])
        self.assertEqual(
            (directory / "files/pages/k1.md").read_text(), "Synthetic knowledge"
        )
        self.assertIn(
            f"kb:{kb.library_id}:knowledge:k1@1",
            (directory / "revisions.json").read_text(),
        )

    def test_peer_history_uses_the_requested_peer_version_within_its_grant(self):
        store = StateStore.create(self.root, config={})
        peer = KnowledgeBase(self.root / "peer")
        for number in range(1, 4):
            peer.commit(
                number - 1,
                [],
                None,
                0,
                f"seed-{number}",
                files={"notes.txt": f"peer-{number}"},
            )
        config = {
            "knowledge_libraries": [
                {"alias": "peer", "path": str(peer.root), "version": 2}
            ],
            "role_definitions": {"plan": {"knowledge_bases": ["study", "peer"]}},
        }
        packet = {"assembly": self.prepare(config=config), "knowledge_config": config}
        with patch(
            "research_loop.runtime.call_context.dispatched_input", return_value=packet
        ):
            self.assertEqual(
                read_at_call(store, "invocation", "peer", "notes.txt", version=1),
                "peer-1",
            )
            with self.assertRaises((ValueError, PermissionError)):
                read_at_call(store, "invocation", "peer", "notes.txt", version=3)

    def test_historical_call_respects_its_recorded_version_and_read_tools(self):
        store = StateStore.create(self.root, config={})
        library = KnowledgeBase(self.root)
        library.commit(0, [], "# first", 0, "first")
        packet = {
            "assembly": {
                "version": 1,
                "knowledge_libraries": library.freeze(),
                "tools": {"read_index": {}},
            },
            "knowledge_config": {},
        }
        library.commit(1, [], "# later", 0, "later")
        with patch(
            "research_loop.runtime.call_context.dispatched_input", return_value=packet
        ):
            self.assertEqual(read_at_call(store, "old", "study", "index.md"), "# first")
            with self.assertRaisesRegex(ValueError, "different library version"):
                read_at_call(store, "old", "study", "index.md", version=2)
            with self.assertRaises(PermissionError):
                read_at_call(store, "old", "study", "some-file")

    def test_legacy_history_denial_requires_explicit_retrieval_authorization(self):
        config = {"role_access": {"plan": {"published_history": False}}}
        assembly = self.prepare(config=config)
        self.assertEqual(assembly["knowledge_libraries"], [])
        config["role_definitions"] = {"plan": {"knowledge_bases": ["study"]}}
        with self.assertRaisesRegex(ValueError, "historical feedback denied"):
            self.prepare(config=config)
        config["role_access"]["plan"]["knowledge_bases"] = ["study"]
        self.assertEqual(
            self.prepare(config=config)["knowledge_libraries"][0]["alias"], "study"
        )

    def test_script_definitions_are_readable_and_require_frozen_science(self):
        from integrations.script_runtime import application as script

        plan = script.resolve_role("plan", {})
        execute = script.resolve_role("execute", {})
        self.assertEqual(plan["output"], "ScriptPlan")
        self.assertEqual(execute["output"], "ScriptExecute")
        self.assertIn("scientific_contract", plan["inputs"])
        self.assertIn("backend_contracts", execute["inputs"])
        self.assertEqual(script.resolve_role("review", {})["output"], "Publication")
        with self.assertRaisesRegex(ValueError, "incompatible role result type"):
            script.resolve_role(
                "plan", {"role_definitions": {"plan": {"output": "Execute"}}}
            )

    def test_preselected_truncation_provenance_survives_assembly(self):
        payload = self.payload(extra={"provided:note": "prefix"})
        source = next(
            s
            for s in payload["package"]["manifest"]["sources"]
            if s["key"] == "provided:note"
        )
        source.update(
            disposition="truncated",
            selection_reason="Frozen upstream policy",
            original_characters=100,
            provided_characters=6,
        )
        assembly = self.prepare(payload)
        actual = next(
            s for s in assembly["input_sources"] if s["key"] == "provided:note"
        )
        self.assertEqual(actual, source)

    def test_empty_draft_index_still_requires_publish_permission(self):
        path = self.root / "draft"
        path.mkdir()
        (path / "index.md").write_text("")
        submitted = {"processed_through": 1}
        with self.assertRaisesRegex(ValueError, "permission for index_markdown"):
            application._collect_draft(path, submitted, {"pages"}, True)
        self.assertEqual(
            application._collect_draft(path, submitted, {"index_markdown"}, True)[
                "files"
            ],
            {"index.md": ""},
        )

    def test_manager_collects_nested_text_and_binary_without_an_index(self):
        path = self.root / "draft"
        (path / "notes").mkdir(parents=True)
        file = path / "notes/result.txt"
        file.write_bytes(b"unlisted\r\n")
        (path / "samples.bin").write_bytes(b"\xff\x00")
        output = {"processed_through": 1, "remove_paths": ["obsolete"]}
        result = application._collect_draft(path, output, {"pages"}, True)
        self.assertEqual(
            result,
            output
            | {
                "files": {
                    "notes/result.txt": "unlisted\r\n",
                    "samples.bin": {"base64": "/wA="},
                }
            },
        )
        with self.assertRaisesRegex(ValueError, "permission for pages"):
            application._collect_draft(path, output, set(), True)
        with self.assertRaisesRegex(ValueError, "writable Manager draft"):
            application._collect_draft(path, output, {"pages"}, False)
        file.unlink()
        file.symlink_to(Path(__file__).resolve())
        with self.assertRaisesRegex(ValueError, "Only regular files"):
            application._collect_draft(path, output, {"pages"}, True)
        access = RoleAccess.resolve(
            {"role_access": {"knowledge_manager": {"actions": ["pages"]}}},
            "knowledge_manager",
        )
        with self.assertRaisesRegex(ValueError, "permission for index_markdown"):
            access.check_output({"files": {}, "remove_paths": ["index.md"]})
