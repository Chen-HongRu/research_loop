"""Role declarations, frozen inputs, grants and previews without model calls."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from integrations.native_runtime import application, policy, role_tools
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.role_access import RoleAccess
from research_loop.runtime.role_assembly import preview_assembly


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
                assembly["tools"]["submit_result"]["parameters"],
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
                "tools": ["read_index", "read", "submit_result"],
            }
        }
        new = self.prepare(config=config)
        self.assertEqual(
            [entry["alias"] for entry in new["knowledge_libraries"]],
            ["study", "literature"],
        )
        self.assertNotIn("search", new["tools"])
        self.assertEqual(
            [entry["alias"] for entry in old["knowledge_libraries"]], ["study"]
        )
        invoke, _ = role_tools.handler(new, study_root=self.root, config=config)
        with self.assertRaisesRegex(ValueError, "frozen role grant"):
            invoke("search", {"query": "synthetic"})
        serialized = json.dumps(preview_assembly(old))
        self.assertNotIn("Public synthetic sources", serialized)
        self.assertNotIn(str(self.root), json.dumps(new))

    def test_frozen_planning_libraries_scoped_before_prompt(self):
        library = KnowledgeBase(self.root)
        manifests = library.freeze()
        hidden = deepcopy(manifests[0])
        hidden.update(alias="hidden", description="Hidden private description")
        payload = self.payload()
        payload["planning_libraries"] = [*manifests, hidden]
        assembly = self.prepare(payload)
        self.assertNotIn("Hidden private description", assembly["prompt"])
        self.assertNotIn(
            "Hidden private description", json.dumps(preview_assembly(assembly))
        )

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

    def test_retrieved_receipt_is_accepted_only_from_successful_read_history(self):
        payload = self.payload()
        assembly = self.prepare(payload)
        payload.update(
            assembly=assembly,
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )
        reference = "artifacts/retrieved.json"
        response = {
            "output": {
                "timeout_seconds": 10,
                "local_evaluation": None,
                "experiment_definition": "Synthetic retrieval",
            },
            "evidence_refs": [reference],
        }

        def run(*args, **kwargs):
            kwargs["history"].append(
                {
                    "event": "success",
                    "request": {"name": "read", "args": {}},
                    "result": {"evidence_ref": reference},
                }
            )
            return response

        with patch("integrations.native_runtime.transport.run", side_effect=run):
            self.assertEqual(
                application.invoke("agent", payload, self.config)["evidence_refs"],
                [reference],
            )

        def rejected(*args, **kwargs):
            kwargs["history"].append(
                {
                    "event": "failure",
                    "request": {"name": "read"},
                    "result": {"evidence_ref": reference},
                }
            )
            return response

        with (
            patch("integrations.native_runtime.transport.run", side_effect=rejected),
            self.assertRaisesRegex(ValueError, "outside the bound manifest"),
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

    def test_retrieved_page_receipt_contains_canonical_fixed_revision(self):
        library = KnowledgeBase(self.root)
        manifests = library.freeze()
        evidence = library.publish_evidence(
            "e1", "external", {"observation": "Synthetic"}
        )
        manifest = library.freeze()
        library.commit(
            base_version=0,
            pages=[
                {
                    "id": "k1",
                    "title": "Synthetic page",
                    "markdown": "Synthetic knowledge",
                    "dependencies": [evidence],
                }
            ],
            index_markdown="# Synthetic Index\n",
            processed_through=1,
            request_id="synthetic",
            manifest=manifest,
        )
        assembly = self.prepare()
        invoke, _ = role_tools.handler(assembly, study_root=self.root, config={})
        result = invoke("read", {"alias": "study", "path": "k1"})
        self.assertEqual(
            result["fixed_ref"],
            {
                "library_id": manifests[0]["library_id"],
                "kind": "knowledge",
                "id": "k1",
                "revision": 1,
            },
        )
        self.assertEqual(result["content"], "Synthetic knowledge")

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

    def test_index_tools_return_body_and_fixed_authorized_dependencies(self):
        library = KnowledgeBase(self.root)
        manifest = library.freeze()
        library.commit(
            base_version=0,
            pages=[
                {
                    "id": "k1",
                    "title": "Synthetic",
                    "markdown": "Synthetic page",
                    "dependencies": [],
                }
            ],
            index_markdown="# Synthetic Index\n",
            processed_through=0,
            request_id="index-test",
            manifest=manifest,
        )
        assembly = self.prepare()
        invoke, _ = role_tools.handler(assembly, study_root=self.root, config={})
        expected = library.index_with_sources(assembly["knowledge_libraries"], "study")
        direct = invoke("read_index", {"alias": "study"})
        opened = invoke("read", {"alias": "study", "path": "index.md"})
        self.assertEqual(direct, opened)
        self.assertEqual(direct["content"], expected["content"])
        self.assertEqual(direct["dependencies"], expected["dependencies"])
        self.assertTrue(
            all(
                isinstance(ref, dict)
                and {"library_id", "kind", "id", "revision"} == set(ref)
                for ref in direct["dependencies"]
            )
        )
        with self.assertRaises(PermissionError):
            invoke("read_index", {"alias": "hidden"})

    def test_manager_empty_index_is_a_write_and_final_proposal_needs_capability(self):
        access = RoleAccess.resolve(
            {"role_access": {"knowledge_manager": {"actions": []}}}, "knowledge_manager"
        )
        with self.assertRaisesRegex(ValueError, "lacks permission for index_markdown"):
            access.check_output(
                {"pages": [], "index_markdown": "", "processed_through": 0}
            )
        payload = self.payload("knowledge_manager")
        config = {
            "role_definitions": {
                "knowledge_manager": {"tools": ["read_index", "read", "submit_result"]}
            }
        }
        assembly = self.prepare(payload, config)
        payload.update(
            assembly=assembly,
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )
        response = {
            "output": {"pages": [], "index_markdown": "", "processed_through": 0},
            "evidence_refs": [],
        }
        with (
            patch("integrations.native_runtime.transport.run", return_value=response),
            self.assertRaisesRegex(
                ValueError, "lacks write capability for index_markdown"
            ),
        ):
            application.invoke("agent", payload, self.config)
        config["role_access"] = {"knowledge_manager": {"actions": ["index_markdown"]}}
        payload["assembly"] = self.prepare(self.payload("knowledge_manager"), config)
        with patch("integrations.native_runtime.transport.run", return_value=response):
            self.assertEqual(
                application.invoke("agent", payload, self.config)["output"][
                    "index_markdown"
                ],
                "",
            )

    def test_manager_combined_draft_respects_frozen_final_action_grant(self):
        payload = self.payload(
            "knowledge_manager",
            extra={"allowed_actions": []},
            protected=["allowed_actions"],
        )
        assembly = self.prepare(
            payload, {"role_access": {"knowledge_manager": {"actions": []}}}
        )
        payload.update(
            assembly=assembly,
            request_budget={"study_root": str(self.root)},
            knowledge_config={},
        )

        def run(*args, **kwargs):
            kwargs["tool_handler"]("edit_index", {"markdown": ""})
            return {
                "output": {"pages": [], "index_markdown": None, "processed_through": 0},
                "evidence_refs": [],
            }

        with (
            patch("integrations.native_runtime.transport.run", side_effect=run),
            self.assertRaisesRegex(ValueError, "lacks permission for index_markdown"),
        ):
            application.invoke("agent", payload, self.config)

    def test_read_cannot_bypass_revoked_index_tool_by_path_or_typed_reference(self):
        library = KnowledgeBase(self.root)
        manifest = library.freeze()
        library.commit(
            base_version=0,
            pages=[],
            index_markdown="# Synthetic Index\n",
            processed_through=0,
            request_id="index-alias-test",
            manifest=manifest,
        )
        assembly = self.prepare(
            config={"role_definitions": {"plan": {"tools": ["read", "submit_result"]}}}
        )
        invoke, _ = role_tools.handler(assembly, study_root=self.root, config={})
        fixed = {
            "library_id": assembly["knowledge_libraries"][0]["library_id"],
            "kind": "index",
            "id": "index.md",
            "revision": 1,
        }
        for path in ("index.md", fixed):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "read_index grant"),
            ):
                invoke("read", {"alias": "study", "path": path})
