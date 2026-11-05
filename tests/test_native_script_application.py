"""Synthetic typed Plan -> Execute -> native script backend, without services."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import test_native_script_backend as fixture
from test_native_runtime_application import assert_prompt_envelope, prompt_parts
from integrations.native_runtime import policy as native
from integrations.script_runtime import application, backend, policy
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.operations import Operations


class NativeScriptApplicationTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the bounded backend fixture, not its test methods.
        backend_fixture = fixture.NativeScriptBackendTests(methodName="runTest")
        backend_fixture.setUp()
        self.addCleanup(backend_fixture.doCleanups)
        self.root = backend_fixture.root
        self.request = deepcopy(backend_fixture.request)
        self.operation = deepcopy(backend_fixture.op) | {
            "revision": 1,
            "request_ref": "artifacts/request.json",
        }
        self.fake_transport = fixture.Transport()
        self.config = {
            "transport": {
                "backend": "pi",
                "executable": "/synthetic/pi",
                "provider": "synthetic",
                "model": "synthetic",
                "reasoning": "medium",
                "timeout_seconds": 5,
            },
            "backend_handlers": {
                "script": {"handler": "script", "config": backend_fixture.config}
            },
            "scientific_contract_source": "provided:science",
        }
        self.contract = {
            "baseline_ref": "synthetic-revision",
            "global_evaluation": None,
            "local_evaluation": None,
            "protocol": "Use only the supplied synthetic fixture",
            "limitations": ["No research result"],
        }
        self.context = {
            "goal": "Synthetic bounded script",
            "global_evaluation": None,
            "task": {"specification": {}},
            "permissions": {},
            "budget": {},
            "provided:science": deepcopy(self.contract),
            "provided:baseline_source": {
                "files": [
                    {"path": "research/base.py", "content": "print('baseline')\n"}
                ]
            },
            "execution_backends": {
                "script": {
                    "version": "1",
                    "environment": {"id": "synthetic", "version": "1"},
                }
            },
            "resource_pools": {"gpu": 1},
            "operation_authorizations": {
                "grant": {
                    "backends": ["script"],
                    "max_operations": 2,
                    "resource_limits": {"gpu": 1},
                    "cost_limits": {"gpu_seconds": 1800},
                    "deadline": None,
                }
            },
        }
        planned = deepcopy(self.request)
        del planned["parameters"]["source_files"]
        self.plan = {
            "timeout_seconds": 600,
            "local_evaluation": None,
            "experiment_definition": "Run the synthetic script with frozen inputs",
            "baseline_ref": self.contract["baseline_ref"],
            "backend": "script",
            "scientific_contract": deepcopy(self.contract),
            "operations": [planned],
        }
        self.execute = {"operations": [deepcopy(self.request)], "artifacts": []}

    def validate(self, role, output, context=None):
        context = self.context if context is None else context
        return application.validate(role, output, context, self.config)

    def package(self, role):
        context = deepcopy(self.context)
        if role == "execute":
            context["plan"] = deepcopy(self.plan)
        snapshot = ContextSnapshot.capture(
            study_id="synthetic",
            revisions={},
            inputs=context,
            sources=[
                {
                    "id": key,
                    "key": key,
                    "ref": "control:" + key,
                    "revision": 1,
                    "reason": "Synthetic frozen input",
                    "allow_feedback": True,
                }
                for key in context
            ],
            protected_keys=list(context),
            allowed_evidence=[],
        )
        return build_context(snapshot, role, None, "synthetic-v1", {})

    def payload(self, action):
        op = deepcopy(self.operation)
        permit = None
        if action == "submit":
            permit = Operations._binding(op) | {
                "submission_token": "native-once",
                "owner_epoch": 1,
                "deadline": op["deadline"],
                "operation_revision": op["revision"] + 1,
                "request_ref": op["request_ref"],
            }
        return {
            "operation_id": op["id"],
            "action": action,
            "operation": op,
            "request": deepcopy(self.request),
            "permit": permit,
        }

    def test_typed_plan_execute_and_real_bounded_prepare_submit(self):
        self.assertEqual(self.validate("plan", self.plan), self.plan)
        context = self.context | {"plan": self.plan}
        self.assertEqual(self.validate("execute", self.execute, context), self.execute)
        self.assertNotIn("source_files", self.plan["operations"][0]["parameters"])
        with patch.object(backend, "SSHTransport", return_value=self.fake_transport):
            prepared = application.invoke(
                "backend", self.payload("prepare"), self.config
            )
            submitted = application.invoke(
                "backend", self.payload("submit"), self.config
            )
        self.assertTrue(prepared["data"]["ready"])
        self.assertEqual(submitted["data"]["outcome"], "accepted")
        self.assertEqual(
            sum(
                command.startswith("sbatch ")
                for command, _, _ in self.fake_transport.calls
            ),
            1,
        )
        self.assertTrue(list((self.root / "journal").glob("*/journal.json")))

    def test_real_runtime_accepts_plan_then_execute_with_frozen_sources(self):
        from research_loop.runtime.contexts import RuntimeContexts
        from research_loop.runtime.files import publish_artifact
        from research_loop.runtime.store import StateStore
        from research_loop.runtime.updates import CallIdentity, Updater, _identity

        root = self.root / "native-study"
        root.mkdir()
        config = {
            key: deepcopy(value)
            for key, value in self.context.items()
            if not key.startswith("provided:") and key not in {"task", "budget"}
        }
        config["context_sources"] = []
        for name, body in (
            ("science", self.contract),
            ("baseline_source", self.context["provided:baseline_source"]),
        ):
            ref = publish_artifact(root, body)
            config["context_sources"].append(
                {
                    "name": name,
                    "ref": ref,
                    "identity": _identity(root, ref),
                    "revision": 1,
                    "roles": ["plan", "execute"],
                    "allow_feedback": True,
                    "required": True,
                }
            )
        store = StateStore.create(root, config=config)
        with store.coordinator() as owner:
            owner.create_task(
                "script-task", {"instruction": "Run the frozen synthetic script"}
            )
            iteration = owner.claim_task("script-task")
            updater = Updater(
                owner,
                validators={
                    role: lambda output, context, role=role: self.validate(
                        role, output, context
                    )
                    for role in ("plan", "execute")
                },
            )
            for role, output in (("plan", self.plan), ("execute", self.execute)):
                attempt = owner.begin_attempt(
                    role,
                    request_id="script-" + role,
                    iteration_id=iteration["id"],
                    expected_revision=iteration["revision"],
                )
                call = CallIdentity.from_attempt(owner.snapshot()["study_id"], attempt)
                package = RuntimeContexts(owner).build(call)
                updater.bind_context(call, package)
                proposal = updater.validate_output(call, output, [])
                updater.apply_update(proposal, proposal.revisions)
                iteration = next(
                    item
                    for item in owner.snapshot()["iterations"]
                    if item["id"] == iteration["id"]
                )
            operations = Operations(owner).list(execute_attempt_id=call.attempt_id)
            self.assertEqual(len(operations), 1)
            self.assertEqual(
                Operations(owner).request(operations[0]["id"]), self.request
            )

    def test_qualitative_plan_execute_requires_no_allocation(self):
        plan = deepcopy(self.plan)
        plan.update(
            operations=[],
            experiment_definition=(
                "Which assumptions remain unsupported by the supplied synthetic "
                "baseline source and frozen protocol? Reason only from that context."
            ),
        )
        execute = {"operations": [], "artifacts": []}
        with (
            patch.object(
                backend,
                "validate_request",
                side_effect=AssertionError("No backend request"),
            ),
            patch.object(
                backend, "invoke", side_effect=AssertionError("No backend dispatch")
            ),
        ):
            self.assertEqual(self.validate("plan", plan), plan)
            self.assertEqual(
                self.validate("execute", execute, self.context | {"plan": plan}),
                execute,
            )
            with self.assertRaisesRegex(ValueError, "match accepted Plan"):
                self.validate("execute", self.execute, self.context | {"plan": plan})
            with self.assertRaises(ValueError):
                self.validate(
                    "execute",
                    execute | {"artifacts": [{"path": "invented", "version": "1"}]},
                    self.context | {"plan": plan},
                )

    def test_execute_changes_only_source_files(self):
        for target, key, replacement in (
            ("request", "timeout_seconds", 599),
            ("request", "resources", {"gpu": 2}),
            ("request", "cost_bounds", {"gpu_seconds": 60}),
            ("request", "backend", "other"),
            ("request", "authorization_id", "other"),
            ("parameters", "baseline_ref", "other"),
            ("execution", "inputs", []),
            ("execution", "outputs", ["different.json"]),
            ("execution", "time_limit_seconds", 60),
            ("execution", "entrypoint", "research/other.py"),
        ):
            output = deepcopy(self.execute)
            request = output["operations"][0]
            value = (
                request
                if target == "request"
                else request["parameters"]
                if target == "parameters"
                else request["parameters"]["execution"]
            )
            value[key] = replacement
            with self.subTest(target=target, key=key), self.assertRaises(ValueError):
                self.validate("execute", output, self.context | {"plan": self.plan})
        with self.assertRaises(ValueError):
            self.validate(
                "execute",
                {"operations": [], "artifacts": []},
                self.context | {"plan": self.plan},
            )

    def test_protected_scope_and_untyped_requests_rejected(self):
        for path in (
            "protected.py",
            "../escape.py",
            "unscoped.py",
            "research/binary.pt",
        ):
            output = deepcopy(self.execute)
            output["operations"][0]["parameters"]["source_files"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate("execute", output, self.context | {"plan": self.plan})
        for changes in (
            {"metrics": {"score": 1}},
            {"artifacts": [{"path": "invented", "version": "1"}]},
        ):
            with self.assertRaises(ValueError):
                self.validate(
                    "execute",
                    self.execute | changes,
                    self.context | {"plan": self.plan},
                )
        invalid = deepcopy(self.plan)
        invalid["operations"][0]["parameters"]["source_files"] = []
        with self.assertRaises(ValueError):
            self.validate("plan", invalid)

    def test_frozen_science_and_actual_resource_bounds(self):
        for key, replacement in (
            ("scientific_contract", self.contract | {"protocol": "changed"}),
            ("baseline_ref", "changed"),
            ("local_evaluation", {}),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.validate("plan", self.plan | {key: replacement})
        for context in ({}, self.context | {"global_evaluation": {"id": "changed"}}):
            with self.assertRaises(ValueError):
                self.validate("plan", self.plan, context)
        for target, key, value in (
            ("request", "cost_bounds", {"gpu_seconds": 61}),
            ("request", "resources", {"gpu": 2}),
            ("request", "timeout_seconds", 419),
            ("execution", "time_limit_seconds", 1801),
            ("execution", "gpu", False),
            ("execution", "gpu", 1),
        ):
            invalid = deepcopy(self.plan)
            request = invalid["operations"][0]
            selected = (
                request if target == "request" else request["parameters"]["execution"]
            )
            selected[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.validate("plan", invalid)

    def test_scientific_contract_uses_exact_json_scalar_types(self):
        context = deepcopy(self.context)
        context["provided:science"]["repeats"] = 1
        plan = deepcopy(self.plan)
        plan["scientific_contract"] = deepcopy(context["provided:science"])
        plan["scientific_contract"]["repeats"] = True
        with self.assertRaisesRegex(ValueError, "frozen scientific"):
            self.validate("plan", plan, context)

    def test_prompts_describe_complete_envelope_with_resolvable_script_refs(self):
        for role, output_model in policy.MODELS.items():
            with self.subTest(role=role):
                assert_prompt_envelope(
                    self,
                    policy.prompt(role, self.package(role).to_dict(), self.config),
                    output_model,
                )

    def test_plan_response_contract_is_after_large_unchanged_context(self):
        self.context["provided:baseline_source"]["files"][0]["content"] += (
            "# synthetic baseline context\n" * 12000
        )
        package = self.package("plan").to_dict()
        prompt = policy.prompt("plan", package, self.config)
        view, footer, schema = prompt_parts(prompt)
        self.assertEqual(view, package)
        self.assertGreater(prompt.index("\nResponse contract:\n"), len(prompt) // 2)
        self.assertIn("Current role: plan", footer)
        self.assertIn(
            "operations and scientific_contract are required sibling fields inside output",
            footer,
        )
        self.assertEqual(set(schema["required"]), {"output", "evidence_refs"})
        required = schema["properties"]["output"]["required"]
        self.assertIn("operations", required)
        self.assertIn("scientific_contract", required)
        self.assertNotIn("evidence_refs", schema["properties"]["output"]["properties"])

    def test_plan_footer_grounds_exact_contract_null_text_and_canonical_input_sources(
        self,
    ):
        source_key = "provided:frozen_task_contract"
        self.config["scientific_contract_source"] = source_key
        mappings = [
            {"source": "hidden_cache", "name": "cache", "mount": "/inputs/cache"},
            {"source": "targets", "name": "targets", "mount": "/inputs/targets"},
        ]
        science = deepcopy(self.contract) | {
            "protocol": "Evidence consists only of the frozen evaluator.",
            "execution_interface": {"input_mappings": mappings},
        }
        self.context[source_key] = science
        self.context["provided:baseline_source"]["files"][0]["content"] += (
            "# unrelated baseline context\n" * 12000
        )
        settings = self.config["backend_handlers"]["script"]["config"]
        settings["input_mappings"] = {
            row["source"]: {
                "path": "/synthetic/" + row["source"],
                "mount": row["mount"],
                "kind": "directory",
            }
            for row in mappings
        }
        plan = deepcopy(self.plan)
        plan["scientific_contract"] = deepcopy(science)
        plan["operations"][0]["parameters"]["execution"]["inputs"] = [
            {"name": row["name"], "source": row["source"]} for row in mappings
        ]
        package = self.package("plan").to_dict()
        before = deepcopy(package)
        prompt = policy.prompt("plan", package, self.config)
        view, footer, schema = prompt_parts(prompt)
        self.assertEqual(package, before)
        self.assertEqual(view, package)
        displayed, after = footer.split("Exact frozen scientific_contract:\n", 1)[
            1
        ].split("\nCanonical input mappings:\n", 1)
        self.assertEqual(json.loads(displayed), science)
        self.assertIsNone(json.loads(displayed)["local_evaluation"])
        self.assertEqual(json.loads(after.split("\n", 1)[0]), mappings)
        self.assertIn("even a single protocol word", footer)
        self.assertIn("Never put a mount path in source", footer)
        self.assertIn("never put operations inside scientific_contract", footer)
        self.assertEqual(set(schema["required"]), {"output", "evidence_refs"})
        self.assertEqual(self.validate("plan", plan), plan)
        for change in ("missing_null", "paraphrased_protocol", "mount_as_source"):
            invalid = deepcopy(plan)
            if change == "missing_null":
                del invalid["scientific_contract"]["local_evaluation"]
            elif change == "paraphrased_protocol":
                invalid["scientific_contract"]["protocol"] = science[
                    "protocol"
                ].replace("only of", "only by")
            else:
                invalid["operations"][0]["parameters"]["execution"]["inputs"][0][
                    "source"
                ] = mappings[0]["mount"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validate("plan", invalid)
        malformed = deepcopy(plan)
        malformed["scientific_contract"]["operations"] = malformed.pop("operations")
        malformed["evidence_refs"] = []
        payload = {
            "call": {"role": "plan", "attempt_id": "synthetic-plan"},
            "context_ref": "synthetic-ref",
            "package": package,
        }
        for response in (
            {"output": malformed},
            {"output": malformed, "evidence_refs": []},
        ):
            with (
                patch.object(application.transport, "run", return_value=response),
                self.assertRaises(ValueError),
            ):
                application.invoke("agent", payload, self.config)

    def test_script_prompts_explain_existing_paths_and_allocation_bounds(self):
        for role in ("plan", "execute"):
            prompt = policy.prompt(role, self.package(role).to_dict(), self.config)
            with self.subTest(role=role):
                for instruction in (
                    "relative research/*.py",
                    "gpu=true",
                    "paths relative to /output",
                    "ceil(time_limit_seconds/60)*60",
                    "execution_interface.lifecycle_headroom_seconds",
                    "Plan.timeout_seconds must be at least every operation timeout_seconds",
                ):
                    self.assertIn(instruction, prompt)

    def test_prompt_public_operation_contract_uses_config_without_private_settings(
        self,
    ):
        config = deepcopy(self.config)
        settings = config["backend_handlers"]["script"]["config"]
        settings.update(
            resource_pool="accelerator",
            cost_unit="allocated_gpu_s",
            staging_headroom_seconds=480,
        )
        for role in ("plan", "execute"):
            prompt = policy.prompt(role, self.package(role).to_dict(), config)
            with self.subTest(role=role):
                contracts = json.loads(
                    prompt.split("Per-backend public operation contracts:\n", 1)[
                        1
                    ].split("\n", 1)[0]
                )
                self.assertEqual(
                    contracts,
                    {
                        "script": {
                            "resources": {"accelerator": 1},
                            "cost_bounds": {
                                "allocated_gpu_s": "ceil(time_limit_seconds/60)*60"
                            },
                            "staging_headroom_seconds": 480,
                            "example": {
                                "time_limit_seconds": 1800,
                                "cost_bounds": {"allocated_gpu_s": 1800},
                                "minimum_timeout_seconds": 2280,
                            },
                        }
                    },
                )
                self.assertIn("no extra CPU or memory keys", prompt)
                self.assertIn(
                    "not lifecycle timeout or a multiple of candidate count", prompt
                )
                for key in (
                    "ssh_target",
                    "remote_root",
                    "baseline_root",
                    "journal_root",
                    "launcher_source",
                ):
                    self.assertNotIn(settings[key], prompt)
                self.assertNotIn(json.dumps(settings), prompt)

    def test_all_roles_transport_only_manifested_context_and_evidence(self):
        empty = {"knowledge": [], "reviews": [], "corrections": []}
        outputs = {
            "plan": self.plan,
            "execute": self.execute,
            "scout": empty,
            "review": empty,
            "advocate": {"proposals": []},
            "judge": {"changes": []},
        }
        for role, output in outputs.items():
            package = self.package(role)
            with (
                self.subTest(role=role),
                patch.object(
                    application.transport,
                    "run",
                    return_value={"output": output, "evidence_refs": []},
                ) as run,
            ):
                result = application.invoke(
                    "agent",
                    {
                        "call": {"role": role, "attempt_id": "synthetic-" + role},
                        "context_ref": "synthetic-ref",
                        "package": package.to_dict(),
                    },
                    self.config,
                )
                self.assertEqual(result["output"], output)
                prompt = run.call_args.args[0]
                self.assertEqual(prompt_parts(prompt)[0], package.to_dict())
                self.assertNotIn(
                    self.config["backend_handlers"]["script"]["config"]["ssh_target"],
                    prompt,
                )
                self.assertIs(
                    application.transport,
                    __import__(
                        "integrations.native_runtime.transport", fromlist=["run"]
                    ),
                )
        package = self.package("plan")
        with (
            patch.object(
                application.transport,
                "run",
                return_value={"output": self.plan, "evidence_refs": ["unmanifested"]},
            ),
            self.assertRaises(ValueError),
        ):
            application.invoke(
                "agent",
                {
                    "call": {"role": "plan", "attempt_id": "bad"},
                    "context_ref": "synthetic-ref",
                    "package": package.to_dict(),
                },
                self.config,
            )
        for role in ("scout", "review", "advocate", "judge"):
            self.assertIs(policy.MODELS[role], native.MODELS[role])

    def test_submit_requires_matching_native_permit_and_no_imported_operation(self):
        for change in (
            lambda p: p.update(permit=None),
            lambda p: p["permit"].update(deadline=1),
            lambda p: p["operation"].update(submit_forbidden=True),
            lambda p: p.update(operation_id="other"),
        ):
            payload = self.payload("submit")
            change(payload)
            with (
                self.assertRaises(ValueError),
                patch.object(
                    backend,
                    "invoke",
                    side_effect=AssertionError("Must reject before dispatch"),
                ),
            ):
                application.invoke("backend", payload, self.config)
        payload = self.payload("submit")
        with patch.object(
            backend, "invoke", return_value={"event_key": "synthetic", "data": {}}
        ) as invoke:
            application.invoke("backend", payload, self.config)
            op = invoke.call_args.args[1]
            self.assertEqual(op["submission_token"], "native-once")
            self.assertEqual(op["submit_owner_epoch"], 1)
        with (
            patch.object(
                Path, "read_text", side_effect=AssertionError("No body reads")
            ),
            patch.object(subprocess, "Popen", side_effect=AssertionError("No process")),
        ):
            self.assertEqual(application.validate_config(self.config), self.config)
