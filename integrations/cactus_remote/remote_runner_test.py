"""Real local worker/process tests; no SSH server, GPU, model or private data."""

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import remote_runner

TRAINER = """import argparse,os,time
from pathlib import Path
p=argparse.ArgumentParser()
for name in ['data','output','lr','epochs','model','seed','batch-size','threads','prediction-splits','device']:
    p.add_argument('--'+name,required=True)
a=p.parse_args()
with (Path(a.data)/'starts').open('a') as f: f.write('started\\n')
assert a.epochs=='1' and a.seed=='0' and a.batch_size=='128' and a.threads=='4'
assert a.model=='tiny-cnn'
assert a.prediction_splits=='dev' and a.device=='cuda:0'
assert os.environ['CUDA_VISIBLE_DEVICES']=='GPU-synthetic'
out=Path(a.output); out.mkdir()
(out/'dev-predictions.csv').write_text('id,has_cactus\\na.png,0.2\\nb.png,0.8\\n')
"""


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copyfile(Path(remote_runner.__file__), self.root / "remote_runner.py")
        self.data = self.root / "public-data"
        self.data.mkdir()
        (self.data / "metadata.json").write_text(
            json.dumps(remote_runner.INPUT_METADATA)
        )
        self.install(TRAINER)

    def install(self, source):
        trainer = self.root / "fixed-train.py"
        trainer.write_text(source)
        self.deployment = {
            "version": 1,
            "remoteRoot": str(self.root),
            "remotePython": sys.executable,
            "remoteData": str(self.data),
            "remoteTrainer": str(trainer),
            "trainerVersion": "sha256:" + hashlib.sha256(source.encode()).hexdigest(),
            "gpu": "GPU-synthetic",
        }
        (self.root / "deployment.json").write_text(json.dumps(self.deployment))
        self.payload = {
            "protocol": remote_runner.PROTOCOL,
            "authorization": "synthetic-authorization",
            "slot": "round-1",
            "candidate": "synthetic-candidate",
            "params": {"learning_rate": 0.001},
            "trainerVersion": self.deployment["trainerVersion"],
            "inputVersion": remote_runner.INPUT_VERSION,
            "evaluation": "synthetic-dev",
            "evaluationVersion": "1",
            "gpu": "GPU-synthetic",
            "expiresAt": int((time.time() + 20) * 1000),
            "remoteDeadline": int((time.time() + 20) * 1000),
            "jobSeconds": 5,
            "executionIdentity": "synthetic-deployment-identity",
            "remoteDeploymentIdentity": remote_runner.deployment_identity(
                self.deployment
            ),
        }

    def rpc(self, action, payload=None):
        payload = payload or self.payload
        key = remote_runner.key_for(payload)
        process = subprocess.run(
            [sys.executable, str(self.root / "remote_runner.py")],
            input=json.dumps(
                {"action": action, "operationKey": key, "payload": payload}
            ),
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        return json.loads(process.stdout)

    def terminal(self):
        until = time.monotonic() + 6
        while time.monotonic() < until:
            result = self.rpc("collect")
            if result.get("result"):
                return result
            time.sleep(0.04)
        self.fail("Original local worker failed to publish a terminal result")

    def test_one_claim_one_real_worker_and_original_job_identity(self):
        first = self.rpc("submit")
        self.assertIn(
            "job",
            first,
            (
                self.root
                / "operations"
                / remote_runner.key_for(self.payload)
                / "worker.log"
            ).read_text(),
        )
        second = self.rpc("submit")
        self.assertEqual(first["job"], second["job"])
        identity = json.loads(first["job"]["jobRef"])
        self.assertEqual(set(identity), {"boot_id", "pid", "starttime", "nonce"})
        terminal = self.terminal()
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual((self.data / "starts").read_text(), "started\n")
        self.assertIn(b"a.png,0.2", base64.b64decode(terminal["predictionsBase64"]))
        changed = {**self.payload, "params": {"learning_rate": 0.01}}
        self.assertFalse(self.rpc("submit", changed)["ok"])
        self.assertEqual((self.data / "starts").read_text(), "started\n")

    def test_unknown_claim_cannot_spawn_or_refresh_deadline(self):
        key = remote_runner.key_for(self.payload)
        directory = self.root / "operations" / key
        directory.mkdir(parents=True)
        claim = {
            "payload": self.payload,
            "deployment": self.deployment,
            "trainerSourceBase64": base64.b64encode(
                (self.root / "fixed-train.py").read_bytes()
            ).decode(),
            "nonce": uuid.uuid4().hex,
            "createdAt": time.time(),
            "deadline": time.time() + 1,
        }
        (directory / "claim.json").write_text(json.dumps(claim))
        first = self.rpc("submit")
        second = self.rpc("submit")
        self.assertEqual(first["state"], "unknown")
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(json.loads((directory / "claim.json").read_text()), claim)
        self.assertFalse((directory / "job.json").exists())
        self.assertFalse((self.data / "starts").exists())

    def test_clock_is_read_only_and_data_deployment_and_metadata_are_pinned(self):
        before = sorted(path.name for path in self.root.iterdir())
        clock = subprocess.run(
            [sys.executable, str(self.root / "remote_runner.py")],
            input='{"action":"clock"}',
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertGreater(json.loads(clock.stdout)["nowMs"], 0)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        metadata = {
            **remote_runner.INPUT_METADATA,
            "counts": {"train": 1, "dev": 1, "test": 1},
        }
        (self.data / "metadata.json").write_text(json.dumps(metadata))
        self.assertFalse(self.rpc("submit")["ok"])
        self.assertFalse((self.data / "starts").exists())
        (self.data / "metadata.json").write_text(
            json.dumps(remote_runner.INPUT_METADATA)
        )
        changed = {
            **self.deployment,
            "remoteData": str(self.root / "other-public-data"),
        }
        (self.root / "deployment.json").write_text(json.dumps(changed))
        self.assertFalse(self.rpc("submit")["ok"])
        self.assertFalse((self.data / "starts").exists())

    def test_worker_executes_claimed_trainer_bytes_and_deployment_after_global_mutation(
        self,
    ):
        key = remote_runner.key_for(self.payload)
        directory = self.root / "operations" / key
        directory.mkdir(parents=True)
        claim = {
            "payload": self.payload,
            "deployment": self.deployment,
            "trainerSourceBase64": base64.b64encode(
                (self.root / "fixed-train.py").read_bytes()
            ).decode(),
            "nonce": uuid.uuid4().hex,
            "createdAt": time.time(),
            "deadline": time.time() + 5,
        }
        (directory / "claim.json").write_text(json.dumps(claim))
        (self.root / "fixed-train.py").write_text(
            "raise RuntimeError('mutated global trainer must not execute')\n"
        )
        other = self.root / "wrong-data"
        other.mkdir()
        (self.root / "deployment.json").write_text(
            json.dumps(
                {
                    **self.deployment,
                    "remoteData": str(other),
                    "remotePython": "/not/executed/python",
                }
            )
        )
        subprocess.run(
            [sys.executable, str(self.root / "remote_runner.py"), "--worker", key],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        terminal = self.rpc("collect")
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual((self.data / "starts").read_text(), "started\n")
        self.assertFalse((other / "starts").exists())

    def test_extra_arguments_nonfinite_and_wrong_deployment_are_rejected(self):
        for params in [
            {"learning_rate": 0.001, "command": "not-allowed"},
            {"learning_rate": float("nan")},
            {"learning_rate": True},
            {"learning_rate": 0.2},
        ]:
            with self.subTest(params=params):
                self.assertFalse(
                    self.rpc("submit", {**self.payload, "params": params})["ok"]
                )
        self.assertFalse(self.rpc("submit", {**self.payload, "gpu": "GPU-other"})["ok"])
        self.assertFalse((self.data / "starts").exists())

    def test_worker_enforces_original_deadline_and_kills_ignoring_descendants(self):
        child = (
            "import sys,signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\np=Path(sys.argv[1])\n"
            "while True:\n with p.open('a') as f:f.write('effect\\n')\n time.sleep(.03)\n"
        )
        slow = (
            TRAINER.split("out=Path")[0]
            + "\nimport signal,subprocess,sys\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            + f"subprocess.Popen([sys.executable,'-c',{child!r},str(Path(a.data)/'effects')])\n"
            + "while True: time.sleep(.05)\n"
        )
        self.install(slow)
        self.payload["jobSeconds"] = 0.8
        started = time.monotonic()
        self.rpc("submit")
        terminal = self.terminal()
        self.assertEqual(terminal["state"], "timed_out")
        self.assertLess(time.monotonic() - started, 4)
        effects = (self.data / "effects").read_bytes()
        self.assertGreater(len(effects), 0)
        time.sleep(0.2)
        self.assertEqual((self.data / "effects").read_bytes(), effects)
        self.assertEqual((self.data / "starts").read_text(), "started\n")
        self.assertEqual(self.rpc("submit")["job"]["jobRef"], terminal["job"]["jobRef"])
        self.assertEqual((self.data / "starts").read_text(), "started\n")


if __name__ == "__main__":
    unittest.main()
