"""Immutable dispatches keep private derivatives out of unauthorized handoffs."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from integrations.native_runtime import application
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.contexts import RuntimeContexts
from research_loop.runtime.dispatch import DispatchLedger
from research_loop.runtime.controls import ControlInbox, receipt, submit_command
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.handoff_scope import (
    authorize_dependencies,
    authorize_publication,
    authorize_task_publication,
    require_bound_assembly,
)
from research_loop.runtime.knowledge import KnowledgeBase
from research_loop.runtime.knowledge_manager import EvidenceIntake, commit_maintenance
from research_loop.runtime.role_access import ROLES, directory_grants
from research_loop.runtime.source_scope import derived_dependencies, source_reference
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity, Updater, _identity


class HandoffScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        private = publish_artifact(self.root, {"text": "PRIVATE_SENTINEL"})
        self.config = {
            "runtime": {
                "worker_slots": 2,
                "worker_adapter": application.APPLICATION_IDENTITY,
            },
            "context_sources": [
                dict(
                    name="private",
                    ref=private,
                    identity=_identity(self.root, private),
                    revision=1,
                    roles=["plan", "advocate", "judge", "knowledge_manager"],
                    allow_feedback=True,
                    required=False,
                )
            ],
        }
        self.store = StateStore.create(self.root, config=self.config)
        manager = self.store.coordinator()
        self.owner = manager.__enter__()
        self.addCleanup(lambda: manager.__exit__(None, None, None))
        self.kb = KnowledgeBase(self.root, config=self.config)
        self.app_config = {
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

    def prepare(self, payload, config=None):
        return application.prepare_agent(
            payload,
            self.app_config,
            study_config=config or self.config,
            study_root=self.root,
        )

    def upstream(self, role="plan", *, no_kb=False, config=None):
        if role == "plan":
            self.owner.create_task("task", {})
            iteration = self.owner.claim_next()
            scope = dict(
                iteration_id=iteration["id"], expected_revision=iteration["revision"]
            )
        else:
            scope = {}
        attempt = self.owner.begin_attempt(role, request_id=role, **scope)
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        package = RuntimeContexts(self.owner).build(call)
        context_ref = Updater(self.owner, validators={}).bind_context(call, package)
        payload = dict(
            call=call.to_dict(), context_ref=context_ref, package=package.to_dict()
        )
        config = config or self.config
        if no_kb:
            config = config | {"role_definitions": {role: {"knowledge_bases": []}}}
        bound = self.prepare(payload, config)
        reference = publish_artifact(self.root, payload | {"assembly": bound})
        DispatchLedger(self.owner).register_call(
            call, reference, application.APPLICATION_IDENTITY, 100000000000
        )
        output_ref = publish_artifact(
            self.root, {"normalized_output": {"summary": "PRIVATE_SENTINEL"}}
        )
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE attempts SET output_ref=?, output_identity_json=? WHERE id=?",
                (
                    output_ref,
                    json.dumps(_identity(self.root, output_ref)),
                    attempt["id"],
                ),
            )
        return attempt, output_ref, bound

    def recipient(self, role, upstream_ref, *, failed_id=None):
        context = {
            "task": None,
            "budget": {},
            "goal": "synthetic",
            "global_evaluation": None,
            "permissions": {},
        }
        if role == "review":
            context.update(plan={"summary": "PRIVATE_SENTINEL"}, execute={})
        elif role == "judge":
            context["advocate_a"] = {"failure_reason": "PRIVATE_SENTINEL"}
        sources = [
            dict(
                id=key,
                key=key,
                ref=(
                    "control:attempt:" + failed_id
                    if key == "advocate_a" and failed_id
                    else upstream_ref
                    if key in {"plan", "advocate_a"}
                    else "control:" + key
                ),
                revision=1,
                reason="Synthetic frozen input",
                allow_feedback=True,
            )
            for key in context
        ]
        snapshot = ContextSnapshot.capture(
            study_id=self.store.snapshot()["study_id"],
            revisions={},
            inputs=context,
            sources=sources,
            protected_keys=list(context),
            allowed_evidence=[],
        )
        return dict(
            call={"role": role},
            context_ref="control:recipient",
            package=build_context(snapshot, role, None, "synthetic", {}).to_dict(),
        )

    def test_review_rejects_restricted_plan_before_prompt_delivery(self):
        _, reference, _ = self.upstream()
        with self.assertRaises(PermissionError):
            self.prepare(self.recipient("review", reference))

    def test_review_cannot_receive_plans_exclusive_peer_directory(self):
        peer = KnowledgeBase(self.root / "peer")
        peer.commit(0, [], "# PRIVATE_SENTINEL", 0, "peer")
        config = self.config | {
            "context_sources": [],
            "knowledge_libraries": [dict(alias="peer", path=str(peer.root), version=1)],
            "role_definitions": {"plan": {"knowledge_bases": ["study", "peer"]}},
        }
        _, reference, _ = self.upstream(config=config)
        with self.assertRaisesRegex(PermissionError, "knowledge grants"):
            self.prepare(self.recipient("review", reference), config)

    def test_authorized_review_cannot_publish_restricted_paraphrase_to_shared_library(
        self,
    ):
        _, reference, bound = self.upstream()
        config = self.config | {
            "context_sources": [
                self.config["context_sources"][0] | {"roles": sorted(ROLES - {"scout"})}
            ],
            # Review gets the upstream value without its own source provider.
            "role_definitions": {
                "review": {
                    "inputs": [
                        "task",
                        "budget",
                        "goal",
                        "evaluation",
                        "permissions",
                        "approved_plan",
                        "execution",
                        "measurements",
                    ]
                }
            },
        }
        review = self.prepare(self.recipient("review", reference), config)
        self.assertEqual(
            review["inherited_dependencies"],
            derived_dependencies(bound, self.kb.library_id),
        )
        output = dict(
            knowledge=[
                dict(
                    kind="interpretation", statement="PRIVATE_SENTINEL", source_refs=[]
                )
            ],
            reviews=[],
            corrections=[],
        )
        intake = EvidenceIntake(self.owner)
        records = {
            "input": {"assembly": review},
            "output": {"normalized_output": output},
        }
        intake.reader = SimpleNamespace(_read_record=lambda ref, _: records[ref])
        intake._ingest(
            dict(
                id=1,
                attempt_id="review",
                iteration_id="synthetic",
                role="review",
                input_ref="input",
                input_identity={},
                payload_ref="output",
                identity={},
            )
        )
        public = self.kb.live(["study"], role="scout")
        directory = Path(public["entries"][0]["path"])
        self.assertFalse((directory / "evidence/observation-1-0.json").exists())
        self.assertEqual(self.kb.status()["evidence_through"], 1)
        manager = self.prepare(self.recipient("knowledge_manager", reference), config)
        self.assertIn("PRIVATE_SENTINEL", json.dumps(manager["inputs"]["maintenance"]))
        self.assertIn(
            source_reference(self.kb.library_id, config["context_sources"][0]),
            manager["inherited_dependencies"],
        )
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with patch(
                "research_loop.runtime.knowledge_manager.dispatched_input",
                return_value={"assembly": manager, "knowledge_config": config},
            ):
                with self.assertRaises(PermissionError):
                    commit_maintenance(
                        self.owner,
                        conn,
                        SimpleNamespace(attempt_id="manager"),
                        {
                            "files": {"notes.txt": "PRIVATE_SENTINEL"},
                            "processed_through": 1,
                        },
                    )
            conn.rollback()
        self.assertEqual(self.kb.head(), 0)
        self.assertFalse((directory / "notes.txt").exists())

    def test_whole_library_handoff_checks_identity_and_fixed_boundary_without_freeze(
        self,
    ):
        peer = KnowledgeBase(self.root / "peer")
        peer.commit(0, [], "# Shared peer", 0, "peer")
        config = self.config | {
            "knowledge_libraries": [
                dict(alias="peer", path=str(peer.root), version=1),
                dict(alias="older", path=str(peer.root), version=0),
            ]
        }
        library = KnowledgeBase(self.root, config=config)
        reference = dict(
            library_id=peer.library_id, kind="library", id="library", revision=1
        )
        with patch.object(
            KnowledgeBase, "freeze", side_effect=AssertionError("freeze")
        ):
            authorize_dependencies(
                library,
                [
                    source_reference(
                        library.library_id, self.config["context_sources"][0]
                    )
                ],
                role="plan",
                aliases=[],
            )
            authorize_dependencies(
                library, [reference], role="review", aliases=["peer"]
            )
            authorize_dependencies(
                library, [reference], role="review", aliases=["peer", "older"]
            )
            authorize_dependencies(
                library, [reference | {"revision": 0}], role="review", aliases=["peer"]
            )
            with self.assertRaises(PermissionError):
                authorize_dependencies(
                    library,
                    [reference | {"revision": 2}],
                    role="review",
                    aliases=["peer"],
                )
            with self.assertRaises(PermissionError):
                authorize_dependencies(
                    library, [reference], role="review", aliases=["study"]
                )
            with self.assertRaises(PermissionError):
                authorize_dependencies(
                    library,
                    [reference | {"kind": "scope"}],
                    role="review",
                    aliases=["peer"],
                )
            with self.assertRaisesRegex(PermissionError, "recipient role review"):
                authorize_publication(
                    library,
                    [reference],
                    {"plan": ["study", "peer"], "review": ["study"]},
                )

    def test_shared_library_publication_uses_its_actual_directory_audience(self):
        reference = dict(
            library_id=self.kb.library_id, kind="library", id="library", revision=0
        )
        authorize_publication(self.kb, [reference], {"review": ["study"]})
        with self.assertRaises(PermissionError):
            authorize_publication(
                self.kb, [reference], {"review": ["study"], "plan": []}
            )

    def test_directory_grants_are_fixed_by_study_access(self):
        changed = self.config | {
            "role_definitions": {
                "scout": {"knowledge_bases": []},
                "plan": {"knowledge_bases": ["study", "peer"]},
            }
        }
        self.assertEqual(directory_grants(changed), directory_grants(self.config))
        fixed = self.config | {
            "role_access": {
                "scout": {"knowledge_bases": []},
                "execute": {"published_history": False},
                "plan": {"knowledge_bases": ["study", "peer"]},
            }
        }
        grants = directory_grants(fixed)
        self.assertEqual(grants["scout"], [])
        self.assertEqual(grants["execute"], [])
        self.assertEqual(grants["plan"], ["study", "peer"])
        self.assertEqual(grants["review"], ["study"])

    def test_old_scout_reader_stays_protected_after_future_role_config_changes(self):
        self.kb.commit(
            0, [], None, 0, "public-baseline", files={"public.txt": "Public baseline"}
        )
        scout = self.prepare(self.recipient("scout", "control:scout"))
        old_directory = Path(scout["filesystem"]["mounts"]["study"]) / "files"
        self.assertEqual((old_directory / "public.txt").read_text(), "Public baseline")
        submit_command(
            self.root,
            "role_config",
            {"role": "scout", "definition": {"knowledge_bases": []}},
            "hide-future-scout",
        )
        with patch(
            "research_loop.runtime.application.load_application",
            return_value=SimpleNamespace(
                invoker=SimpleNamespace(validate_roles=application.validate_roles)
            ),
        ):
            ControlInbox(self.owner).drain()
        self.assertEqual(receipt(self.root, "hide-future-scout")["status"], "applied")
        config = self.owner.snapshot()["config"]
        self.assertEqual(
            self.prepare(self.recipient("scout", "control:scout"), config)[
                "knowledge_libraries"
            ],
            [],
        )
        private_ref = source_reference(
            self.kb.library_id, self.config["context_sources"][0]
        )
        self.kb.publish_evidence(
            "private-observation",
            "review",
            "PRIVATE_SENTINEL",
            source_refs=[private_ref],
        )
        manager = self.prepare(
            self.recipient("knowledge_manager", "control:manager"), config
        )
        with self.store._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with patch(
                "research_loop.runtime.knowledge_manager.dispatched_input",
                return_value={"assembly": manager, "knowledge_config": config},
            ):
                with self.assertRaises(PermissionError):
                    commit_maintenance(
                        self.owner,
                        conn,
                        SimpleNamespace(attempt_id="manager-after-role-config"),
                        {
                            "files": {"public.txt": "PRIVATE_SENTINEL"},
                            "processed_through": 1,
                        },
                    )
            conn.rollback()
        self.assertEqual(self.kb.head(), 1)
        self.assertEqual((old_directory / "public.txt").read_text(), "Public baseline")

    def test_future_definitions_cannot_expand_judge_shared_peer_authority(self):
        peer = KnowledgeBase(self.root / "peer")
        peer.commit(0, [], "# Private peer", 0, "peer")
        config = self.config | {
            "context_sources": [],
            "knowledge_libraries": [dict(alias="peer", path=str(peer.root), version=1)],
            "role_definitions": {
                role: {"knowledge_bases": ["study", "peer"]} for role in ROLES
            },
        }
        attempt, _, _ = self.upstream("judge", config=config)
        with self.assertRaises(PermissionError):
            authorize_task_publication(self.owner, attempt["id"], config)

    def test_shared_peer_content_requires_fixed_grants_for_every_possible_reader(self):
        peer = KnowledgeBase(self.root / "fixed-peer")
        peer.commit(0, [], "# Fixed peer content", 0, "peer")
        config = {
            "knowledge_libraries": [dict(alias="peer", path=str(peer.root), version=1)],
            "role_access": {
                role: {"knowledge_bases": ["study", "peer"]} for role in ROLES
            },
        }
        consumer = KnowledgeBase(self.root / "shared-peer-consumer", config=config)
        consumer.commit(
            0,
            [],
            None,
            0,
            "explicit-shared-peer",
            files={"notes.txt": "Fixed peer content"},
            file_dependencies=[
                dict(
                    library_id=peer.library_id, kind="library", id="library", revision=1
                )
            ],
        )
        scout = consumer.live(["study"], role="scout")
        directory = Path(scout["entries"][0]["path"]) / "files"
        self.assertEqual((directory / "notes.txt").read_text(), "Fixed peer content")

    def test_failed_advocate_summary_has_original_authority(self):
        attempt, reference, _ = self.upstream("advocate")
        denied = self.config | {"role_access": {"judge": {"context_sources": []}}}
        with self.assertRaises(PermissionError):
            self.prepare(
                self.recipient("judge", reference, failed_id=attempt["id"]), denied
            )

    def test_shared_task_pool_cannot_launder_judge_material(self):
        attempt, _, _ = self.upstream("judge")
        with self.assertRaises(PermissionError):
            authorize_task_publication(self.owner, attempt["id"], self.config)

    def test_no_kb_roles_can_handoff_authorized_scientific_inputs(self):
        _, reference, _ = self.upstream(no_kb=True)
        config = self.config | {
            "context_sources": [
                self.config["context_sources"][0] | {"roles": sorted(ROLES)}
            ],
            "role_definitions": {"review": {"knowledge_bases": []}},
        }
        bound = self.prepare(self.recipient("review", reference), config)
        self.assertEqual(bound["knowledge_libraries"], [])
        self.assertTrue(bound["inherited_dependencies"])

    def test_old_native_intake_is_rejected_without_publication(self):
        intake = EvidenceIntake(SimpleNamespace(store=self.store))
        records = {
            "input": {"assembly": {"version": 1}},
            "output": {
                "normalized_output": {"knowledge": [{"statement": "PRIVATE_SENTINEL"}]}
            },
        }
        intake.reader = SimpleNamespace(_read_record=lambda ref, _: records[ref])
        with self.assertRaisesRegex(ValueError, "original deployment"):
            intake._ingest(
                dict(
                    id=1,
                    input_ref="input",
                    input_identity={},
                    payload_ref="output",
                    identity={},
                )
            )
        self.assertEqual(self.kb.status()["evidence_through"], 0)
        with self.assertRaisesRegex(ValueError, "original deployment"):
            require_bound_assembly({"version": 1})

        records["input"] = {}
        with self.assertRaisesRegex(ValueError, "original deployment"):
            intake._ingest(
                dict(
                    id=1,
                    input_ref="input",
                    input_identity={},
                    payload_ref="output",
                    identity={},
                    adapter=application.APPLICATION_IDENTITY,
                )
            )
        self.assertEqual(self.kb.status()["evidence_through"], 0)
