"""Real executable Pi-stub transport, no model/network/GPU calls."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from integrations.native_runtime import application, policy
from research_loop.harness import HarnessError
from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.invocations import LocalProcessInvoker


OUTPUTS = {
    "plan": {
        "timeout_seconds": 20,
        "local_evaluation": None,
        "experiment_definition": "Compare only the supplied qualitative evidence; no execution.",
    },
    "execute": {"operations": [], "artifacts": []},
    "advocate": {
        "proposals": [
            {
                "id": "followup",
                "specification": {"instruction": "Inspect uncertainty"},
                "priority": 1,
                "dependencies": [],
                "reason": "The evidence remains limited",
            }
        ]
    },
    "judge": {
        "changes": [
            {
                "action": "create",
                "id": "followup",
                "specification": {"instruction": "Inspect uncertainty"},
                "priority": 1,
                "dependencies": [],
            }
        ]
    },
    "scout": {
        "knowledge": [
            {
                "id": "source-note",
                "expected_revision": 0,
                "kind": "external",
                "statement": "The supplied source reports a synthetic observation",
                "conditions": ["Synthetic fixture"],
                "source_refs": ["source:used"],
                "measurement_ids": [],
                "interpretation": "",
                "limitations": ["No scientific evidence"],
            }
        ],
        "reviews": [],
        "corrections": [],
    },
    "review": {
        "knowledge": [],
        "reviews": [
            {
                "measurement_id": "m",
                "valid": False,
                "verified": False,
                "reason": "Synthetic evaluator lacks independent verification",
            }
        ],
        "corrections": [],
    },
}


class NativeApplicationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {
            "transport": {
                "backend": "pi",
                "executable": str(self.root / "pi-stub"),
                "provider": "openai-codex",
                "model": "synthetic-model",
                "reasoning": "medium",
                "timeout_seconds": 5,
                "private_calls_dir": str(self.root / "private-calls"),
            },
            "backend_handlers": {},
        }
        self.stub()

    def stub(
        self,
        *,
        model="synthetic-model",
        thinking="medium",
        catalog="synthetic-model",
        response=None,
        hanging=False,
        spawn_child=True,
        extra_blocks=None,
    ):
        script = f"""#!{sys.executable}
import json, os
from pathlib import Path
import subprocess, sys, time
arguments = sys.argv[1:]
with Path({str(self.root / "arguments.jsonl")!r}).open('a') as stream:
    stream.write(json.dumps(arguments)+'\\n')
assert '--no-tools' in arguments
assert '--no-context-files' in arguments
assert arguments[arguments.index('--system-prompt')+1].startswith('You are a tool-free research assistant.')
assert arguments[arguments.index('--append-system-prompt')+1] == ''
assert '--no-mcp' in arguments and '--no-extensions' in arguments
if '--list-models' in arguments:
    assert '--offline' in arguments
    print('openai-codex {catalog}')
    sys.exit(0)
assert arguments[arguments.index('--provider')+1] == 'openai-codex'
assert arguments[arguments.index('--model')+1] == 'synthetic-model'
assert arguments[arguments.index('--thinking')+1] == 'medium'
prompt = sys.stdin.read()
package = json.loads(prompt.split('ContextPackage:\\n', 1)[1])
role = package['manifest']['role']
assert package['context']['goal'] == 'Synthetic native roles'
if {hanging!r}:
    if {spawn_child!r}:
        subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(3); Path("+repr({str(self.root / "late-child")!r})+").write_text('late')"])
    Path({str(self.root / "cli-ready")!r}).write_text(str(os.getpgid(0)))
    time.sleep(30)
outputs = {OUTPUTS!r}
refs = {{'scout':['source:used'], 'review':['receipt:m']}}.get(role, [])
answer = {response!r} if {response is not None!r} else {{'output': outputs[role], 'evidence_refs':refs}}
message = {{'role':'assistant','stopReason':'stop','provider':'openai-codex','model':{model!r},'thinkingLevel':{thinking!r},
           'content':{extra_blocks or []!r}+[{{'type':'text','text':json.dumps(answer)}}]}}
print(json.dumps({{'type':'message_end','message':message}}))
print(json.dumps({{'type':'agent_settled','aborted':False}}))
"""
        executable = Path(self.config["transport"]["executable"])
        executable.write_text(script)
        executable.chmod(0o700)

    def package(self, role):
        inputs = {
            "goal": "Synthetic native roles",
            "permissions": {},
            "task": {"specification": {}},
            "budget": {},
            "measurement:m": {"id": "m", "receipt_ref": "receipt:m"},
            "source": "Synthetic supplied source",
            "unused": "Uncited supplied source",
        }
        refs = {
            "measurement:m": "receipt:m",
            "source": "source:used",
            "unused": "source:unused",
        }
        snapshot = ContextSnapshot.capture(
            study_id="synthetic",
            revisions={},
            inputs=inputs,
            sources=[
                {
                    "id": key,
                    "key": key,
                    "ref": refs.get(key, "control:" + key),
                    "revision": 1,
                    "reason": "Synthetic exact package",
                    "allow_feedback": True,
                }
                for key in inputs
            ],
            protected_keys=["goal", "permissions", "task", "budget"]
            + (["measurement:m"] if role == "review" else []),
            allowed_evidence=list(refs.values()),
        )
        return build_context(snapshot, role, None, "native-fixture-v1", {})

    def payload(self, role):
        return {
            "call": {"role": role, "attempt_id": "synthetic-" + role},
            "context_ref": "synthetic-context",
            "package": self.package(role).to_dict(),
        }

    def test_all_six_roles_use_real_cli_stub_and_exact_package(self):
        for role in OUTPUTS:
            with self.subTest(role=role):
                result = application.invoke("agent", self.payload(role), self.config)
                self.assertEqual(result["output"], OUTPUTS[role])
                self.assertEqual(
                    application.validate(
                        role, result["output"], self.package(role).context, self.config
                    ),
                    OUTPUTS[role],
                )
                self.assertNotIn("source:unused", result["evidence_refs"])
        arguments = [
            json.loads(row)
            for row in (self.root / "arguments.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(arguments), 12)
        self.assertEqual(sum("--list-models" in row for row in arguments), 6)
        self.assertEqual(
            len(
                list((self.root / "private-calls").glob("call-*/response.stdout.jsonl"))
            ),
            6,
        )

    def test_catalog_or_effective_identity_mismatch_never_falls_back(self):
        self.stub(catalog="other-model")
        with self.assertRaises(HarnessError):
            application.invoke("agent", self.payload("plan"), self.config)
        self.assertEqual(
            len((self.root / "arguments.jsonl").read_text().splitlines()), 1
        )
        for overrides in ({"model": "other-model"}, {"thinking": "low"}):
            self.stub(**overrides)
            with self.subTest(overrides=overrides), self.assertRaises(HarnessError):
                application.invoke("agent", self.payload("plan"), self.config)

    def test_invalid_config_fails_purely_before_any_cli(self):
        for field, value in (
            ("backend", "codex"),
            ("timeout_seconds", 0),
            ("model", " "),
        ):
            config = deepcopy(self.config)
            config["transport"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                application.validate_config(config)
        self.assertFalse((self.root / "arguments.jsonl").exists())

    def test_extra_claims_and_unknown_evidence_are_rejected(self):
        for role, extra in (
            ("execute", {"metrics": {"score": 1}}),
            ("plan", {"state_after": {}}),
        ):
            with self.subTest(role=role), self.assertRaises(ValueError):
                application.validate(
                    role, OUTPUTS[role] | extra, self.package(role).context, self.config
                )
        for output in (
            {"operations": [{"id": "train"}], "artifacts": []},
            {"operations": [], "artifacts": [{"path": "made-up", "version": "1"}]},
        ):
            with self.assertRaises(ValueError):
                policy.validate_output("execute", output, {})
        self.stub(
            response={"output": OUTPUTS["scout"], "evidence_refs": ["source:unknown"]}
        )
        with self.assertRaises(ValueError):
            application.invoke("agent", self.payload("scout"), self.config)
        self.stub(response={"output": OUTPUTS["scout"], "evidence_refs": []})
        with self.assertRaises(ValueError):
            application.invoke("agent", self.payload("scout"), self.config)

    def test_local_timeout_reaps_cli(self):
        self.stub(hanging=True, spawn_child=False)
        self.config["transport"]["timeout_seconds"] = 0.15
        with self.assertRaises(subprocess.TimeoutExpired):
            application.invoke("agent", self.payload("plan"), self.config)

    def test_supervised_cancellation_retires_cli_group_and_descendant(self):
        self.stub(hanging=True)
        (self.root / "runtime").mkdir()
        module = Path(application.__file__)
        invoker = LocalProcessInvoker(
            self.root / "runtime", module, application.APPLICATION_IDENTITY, self.config
        )
        reference = publish_artifact(self.root / "runtime", self.payload("plan"))
        invoker.launch("call-synthetic", "agent", reference, time.time() + 8)
        deadline = time.monotonic() + 4
        while not (self.root / "cli-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue((self.root / "cli-ready").exists())
        invoker.cancel("call-synthetic", "Synthetic cancellation")
        while time.monotonic() < deadline + 4:
            observed = invoker.inspect("call-synthetic")
            if observed["completion_ref"]:
                break
            time.sleep(0.02)
        self.assertTrue(observed["completion_ref"])
        completion = json.loads(
            (self.root / "runtime" / observed["completion_ref"]).read_text()
        )
        self.assertEqual(completion["status"], "cancelled")
        self.assertTrue(completion["sender_retired"])
        time.sleep(3.1)
        self.assertFalse((self.root / "late-child").exists())

    def test_native_corrections_and_judge_engine_contract(self):
        correction = {
            "knowledge": [],
            "reviews": [],
            "corrections": [
                {
                    "measurement_id": "m",
                    "expected_revision": 1,
                    "valid": False,
                    "verified": False,
                    "reason": "Synthetic correction",
                    "evidence_refs": ["receipt:m"],
                }
            ],
        }
        self.stub(response={"output": correction, "evidence_refs": ["receipt:m"]})
        self.assertEqual(
            application.validate(
                "review", correction, self.package("review").context, self.config
            ),
            correction,
        )
        with self.assertRaises(ValueError):
            application.invoke("agent", self.payload("scout"), self.config)
        for output in (correction, OUTPUTS["review"]):
            with self.subTest(output=output), self.assertRaises(ValueError):
                application.validate(
                    "scout", output, self.package("scout").context, self.config
                )
        schema = policy.MODELS["scout"].model_json_schema()["properties"]
        self.assertEqual(schema["reviews"]["maxItems"], 0)
        self.assertEqual(schema["corrections"]["maxItems"], 0)
        self.assertNotIn(
            "maxItems",
            policy.MODELS["review"].model_json_schema()["properties"]["corrections"],
        )
        correction["corrections"][0]["evidence_refs"] = []
        with self.assertRaises(ValueError):
            application.validate(
                "review", correction, self.package("review").context, self.config
            )
        self.stub(
            response={
                "output": {"knowledge": [], "reviews": [], "corrections": []},
                "evidence_refs": [],
            }
        )
        with self.assertRaisesRegex(ValueError, "every own measurement"):
            application.invoke("agent", self.payload("review"), self.config)
        for output in (
            {"changes": [{"action": "unknown", "id": "task", "expected_revision": 1}]},
            {"changes": OUTPUTS["judge"]["changes"] * 2},
        ):
            with self.assertRaises(ValueError):
                application.validate("judge", output, {}, self.config)

    def test_local_evaluation_with_nullable_global_requires_route_and_valid_protocol(
        self,
    ):
        from examples.runtime_publication import EVALUATION

        plan = deepcopy(OUTPUTS["plan"])
        plan["local_evaluation"] = deepcopy(EVALUATION)
        context = {
            "global_evaluation": None,
            "task": {"specification": {"route_id": "local-route"}},
        }
        self.assertEqual(application.validate("plan", plan, context, self.config), plan)
        with self.assertRaisesRegex(ValueError, "route"):
            application.validate(
                "plan", plan, {"global_evaluation": None, "task": None}, self.config
            )
        invalid = deepcopy(plan)
        invalid["local_evaluation"]["data_role"] = "final"
        with self.assertRaisesRegex(ValueError, "final"):
            application.validate("plan", invalid, context, self.config)
        context["global_evaluation"] = deepcopy(EVALUATION)
        with self.assertRaisesRegex(ValueError, "global ID"):
            application.validate("plan", plan, context, self.config)

    def test_loader_rejects_invalid_native_config_before_runtime_creation_or_invocation(
        self,
    ):
        from research_loop.runtime.application import prepare_application

        invalid = deepcopy(self.config)
        invalid["transport"]["backend"] = "codex"
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(ValueError, "Only the Pi"):
            prepare_application(
                application.__file__, invalid, application.APPLICATION_IDENTITY
            )
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse((self.root / "arguments.jsonl").exists())
        self.assertFalse(list(self.root.rglob("*.sqlite*")))
        self.assertFalse((self.root / "invocations").exists())

    def test_nonempty_native_interpretation_passes_real_context_update_and_publication(
        self,
    ):
        from research_loop.runtime.contexts import RuntimeContexts
        from research_loop.runtime.publication import Publisher
        from research_loop.runtime.store import StateStore
        from research_loop.runtime.updates import CallIdentity, Updater

        output = {
            "knowledge": [
                {
                    "id": "uncertain-interpretation",
                    "expected_revision": 0,
                    "kind": "interpretation",
                    "statement": "The supplied synthetic task remains unresolved",
                    "conditions": ["Only synthetic context available"],
                    "source_refs": [],
                    "measurement_ids": [],
                    "interpretation": "No scientific conclusion follows",
                    "limitations": ["No experiment performed"],
                }
            ],
            "reviews": [],
            "corrections": [],
        }
        self.stub(response={"output": output, "evidence_refs": []})
        store = StateStore.create(
            self.root / "study",
            config={
                "goal": "Synthetic native roles",
                "permissions": {},
                "global_evaluation": None,
            },
        )
        with store.coordinator() as owner:
            attempt = owner.begin_attempt("scout", request_id="native-interpretation")
            call = CallIdentity.from_attempt(owner.snapshot()["study_id"], attempt)
            package = RuntimeContexts(owner).build(call)
            updater = Updater(
                owner,
                validators={
                    "scout": lambda result, context: application.validate(
                        "scout", result, context, self.config
                    )
                },
            )
            context_ref = updater.bind_context(call, package)
            result = application.invoke(
                "agent",
                {
                    "call": call.to_dict(),
                    "context_ref": context_ref,
                    "package": package.to_dict(),
                },
                self.config,
            )
            proposal = updater.validate_output(
                call, result["output"], result["evidence_refs"]
            )
            updater.apply_update(proposal, proposal.revisions)
            receipt = Publisher(owner).publish(call)
            self.assertEqual(
                receipt["knowledge"],
                [{"id": "uncertain-interpretation", "revision": 1}],
            )
            snapshot = Publisher(owner).snapshot()
            self.assertEqual(snapshot["knowledge"][0]["kind"], "interpretation")
            self.assertEqual(snapshot["eligibility"], [])

    def test_thinking_is_private_and_tool_calls_remain_forbidden(self):
        blocks = [
            {"type": "thinking", "thinking": "Synthetic private reasoning"},
            {
                "type": "thinking",
                "thinking": "",
                "redacted": True,
                "thinkingSignature": "synthetic-private-signature",
            },
        ]
        self.stub(extra_blocks=blocks)
        payload = self.payload("plan")
        result = application.invoke("agent", payload, self.config)
        self.assertEqual(result, {"output": OUTPUTS["plan"], "evidence_refs": []})
        self.assertNotIn("private reasoning", json.dumps(result))
        folders = list(
            (self.root / "private-calls").glob("call-synthetic-plan-*/invocation.json")
        )
        self.assertEqual(len(folders), 1)
        self.assertEqual(
            json.loads(folders[0].read_text()),
            {"call": payload["call"], "context_ref": payload["context_ref"]},
        )
        self.assertEqual(folders[0].parent.stat().st_mode & 0o777, 0o700)
        for block in (
            {
                "type": "toolCall",
                "id": "synthetic-tool",
                "name": "bash",
                "arguments": {},
            },
            {"type": "unknownActive", "text": "Unsupported"},
        ):
            self.stub(extra_blocks=[block])
            with self.subTest(block=block["type"]), self.assertRaises(HarnessError):
                application.invoke("agent", payload, self.config)

    @unittest.skipUnless(
        os.environ.get("NATIVE_PI_DIST") and shutil.which("node"),
        "Optional installed Pi source check",
    )
    def test_installed_pi_loader_disables_ambient_context_and_system_files(self):
        from integrations.native_runtime.transport import SYSTEM_PROMPT

        dist = Path(os.environ["NATIVE_PI_DIST"]).resolve()
        project, agent = self.root / "synthetic-project", self.root / "synthetic-agent"
        (project / ".pi").mkdir(parents=True)
        agent.mkdir()
        for folder in (project, project / ".pi", agent):
            for name in ("AGENTS.md", "CLAUDE.md", "SYSTEM.md", "APPEND_SYSTEM.md"):
                (folder / name).write_text("SYNTHETIC AMBIENT INPUT MUST NOT BE READ")
        script = r"""
import fs from 'node:fs';
import {syncBuiltinESMExports} from 'node:module';
import {pathToFileURL} from 'node:url';
const [dist, project, agent, system] = process.argv.slice(1);
const {parseArgs} = await import(pathToFileURL(dist+'/cli/args.js'));
const {DefaultResourceLoader} = await import(pathToFileURL(dist+'/core/resource-loader.js'));
const {SettingsManager} = await import(pathToFileURL(dist+'/core/settings-manager.js'));
const flags = parseArgs(['--no-context-files','--no-extensions','--no-skills','--no-prompt-templates',
    '--no-mcp','--no-tools','--system-prompt',system,'--append-system-prompt','']);
const loader = new DefaultResourceLoader({cwd:project,agentDir:agent,settingsManager:SettingsManager.inMemory(),
    noContextFiles:flags.noContextFiles,noExtensions:flags.noExtensions,noSkills:flags.noSkills,
    noPromptTemplates:flags.noPromptTemplates,systemPrompt:flags.systemPrompt,appendSystemPrompt:flags.appendSystemPrompt});
// Positive discovery check proves the synthetic override files would be found.
if (!loader.discoverSystemPromptFile().endsWith('SYSTEM.md')) throw Error('Missing synthetic system discovery');
if (!loader.discoverAppendSystemPromptFile().endsWith('APPEND_SYSTEM.md')) throw Error('Missing synthetic append discovery');
const empty = () => ({extensions:[],skills:[],prompts:[],themes:[],installFailures:[]});
loader.packageManager.resolve = async () => empty();
loader.packageManager.resolveExtensionSources = async () => empty();
const read = fs.readFileSync;
fs.readFileSync = function(path,...args) {
    if (/(?:AGENTS|CLAUDE|SYSTEM|APPEND_SYSTEM)\.md$/.test(String(path))) throw Error('Ambient context read attempted');
    return read.call(this,path,...args);
};
syncBuiltinESMExports();
await loader.reload();
if (loader.getAgentsFiles().agentsFiles.length !== 0) throw Error('Ambient agents loaded');
if (loader.getSystemPrompt() !== system) throw Error('System override ignored');
if (loader.getAppendSystemPrompt().join('') !== '') throw Error('Ambient append loaded');
if (loader.getSkills().skills.length || loader.getPrompts().prompts.length || loader.getExtensions().extensions.length)
    throw Error('Ambient resources loaded');
console.log(JSON.stringify({system:loader.getSystemPrompt(),context_files:0,append:loader.getAppendSystemPrompt()}));
"""
        result = subprocess.run(
            [
                "node",
                "--input-type=module",
                "-e",
                script,
                str(dist),
                str(project),
                str(agent),
                SYSTEM_PROMPT,
            ],
            cwd=project,
            env={
                **os.environ,
                "HOME": str(self.root),
                "PI_CODING_AGENT_DIR": str(agent),
            },
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["system"], SYSTEM_PROMPT)

    def test_loader_validates_frozen_cleanup_config_before_invocation(self):
        from research_loop.runtime.application import prepare_application

        plan_dir = "iterations/0001/plan/attempt-001"
        execute_dir = "iterations/0001/execute/attempt-001"
        plan = {
            "decision": "investigate",
            "base_experiment": None,
            "timeout_seconds": 20,
            "execution": {"mode": "script", "gpu": True, "time_limit_seconds": 10},
        }
        bodies = {
            "grant.json": {
                "grant_id": "synthetic-original-grant",
                "gpu_seconds": 60,
                "deadline": 1000,
            },
            plan_dir + "/canonical-response.json": plan,
            plan_dir + "/response.json": plan,
            plan_dir + "/completed.json": {
                "version": 1,
                "run_id": "synthetic-plan",
                "response": plan,
            },
            plan_dir + "/process.json": {"run_id": "synthetic-plan"},
            execute_dir + "/parameter-budget.json": {"deadline": 900},
        }
        inventory = {
            "source_root": "/synthetic/legacy",
            "native_state": {
                "iteration": 1,
                "calls": {"execute": execute_dir},
                "plan_ref": plan_dir + "/canonical-response.json",
            },
            "records": {
                key: {"identity": {"size": 1}, "text": json.dumps(body)}
                for key, body in bodies.items()
            },
        }
        cleanup = {
            "inventory": inventory,
            "backend_controls": {
                "version": 1,
                "authorization_ref": "grant.json",
                "accounting": {
                    "settled_gpu_seconds": 0,
                    "admitted_operations": 0,
                    "source_refs": ["grant.json"],
                },
                "jobs": [],
                "current_source_refs": ["grant.json"],
            },
            "receipt_path": "/synthetic/controller/receipt.json",
        }
        config = deepcopy(self.config)
        config["backend_handlers"] = {
            "original-backend": {"handler": "protok_cleanup", "config": cleanup}
        }
        with (
            patch.object(
                Path, "read_text", side_effect=AssertionError("No external body reads")
            ),
            patch.object(
                subprocess, "Popen", side_effect=AssertionError("No CLI calls")
            ),
        ):
            spec = prepare_application(
                application.__file__, config, application.APPLICATION_IDENTITY
            )
        self.assertEqual(spec["config"], config)
        before = set(self.root.iterdir())
        config["backend_handlers"]["original-backend"]["config"] = {}
        with self.assertRaisesRegex(ValueError, "Cleanup requires"):
            prepare_application(
                application.__file__, config, application.APPLICATION_IDENTITY
            )
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertFalse((self.root / "arguments.jsonl").exists())
        self.assertFalse((self.root / "invocations").exists())

    def test_unconfigured_or_native_backend_can_never_route_cleanup(self):
        for action in ("submit", "prepare", "inspection"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                application.invoke(
                    "backend",
                    {
                        "action": action,
                        "operation": {"backend": "unknown", "submit_forbidden": False},
                    },
                    self.config,
                )
