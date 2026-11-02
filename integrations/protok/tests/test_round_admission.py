"""Entry cutoffs admit whole rounds, with crash-safe state binding."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from integrations.protok import round_admission as rounds
from integrations.protok.run import ProTokHarness, admission
from integrations.protok.backend import ProTokBackend, ProTokError
from research_loop import Study, initialize
from research_loop.harness import HarnessConfig
from test_core import RecordedHarness


class RoundAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.owner = {
            "owner_token": "owner",
            "project": "/canonical",
            "handoff_acknowledged": True,
            "expires_at": 1100,
        }
        self.lease = {
            "schema": 2,
            "owner_token": "owner",
            "started_at": 100,
            "deadline": 1000,
            "seconds": 86400,
            "additional_gpu_seconds": 86400,
            "max_new_candidates": None,
            "authorization": "bounded short research",
        }
        self.write("control/ownership.json", self.owner)
        self.write("control/continuation.json", self.lease)
        self.clock = [999.0]
        clock = patch.object(rounds.time, "time", side_effect=lambda: self.clock[0])
        clock.start()
        self.addCleanup(clock.stop)
        self.backend = SimpleNamespace(
            owner_token="owner",
            project=Path("/canonical"),
            inspect=lambda: {"outstanding": [], "research_outstanding": []},
        )

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def bind(self, iteration="0001", phase="execute"):
        token = rounds.admit(self.root, iteration, owner_token="owner")
        committed_at = rounds.commit(self.root, iteration, token, owner_token="owner")
        self.write(
            f"study/iterations/{iteration}/iteration.json",
            {
                "id": iteration,
                "round_admission": token,
            },
        )
        self.write(
            "study/state.json",
            {
                "iteration": int(iteration),
                "phase": phase,
                "status": "ready",
                "round_admission": token,
                "round_admitted_at": committed_at,
            },
        )
        return token

    def test_committed_round_survives_cutoff_and_reuses_token(self):
        token = self.bind()
        self.clock[0] = 1200
        self.assertEqual(rounds.admit(self.root, "0001", owner_token="owner"), token)
        self.assertEqual(admission(self.root, self.backend), 1000)
        self.assertEqual(
            rounds.execution_deadline(self.root, 1000, 60, attempt="existing"), 1260
        )
        # An independent Plan/attempt deadline is never extended.
        self.assertEqual(
            rounds.execution_deadline(self.root, 990, 60, attempt="plan"), 990
        )
        with self.assertRaises(ValueError):
            rounds.admit(self.root, "0002", owner_token="owner")

    def test_same_attempt_window_is_never_renewed_after_restart(self):
        self.bind()
        self.clock[0] = 1200
        first = rounds.execution_deadline(self.root, 1000, 60, attempt="research/0001")
        self.clock[0] = 1300
        self.assertEqual(
            rounds.execution_deadline(self.root, 1000, 3600, attempt="research/0001"),
            first,
        )
        self.assertEqual(first, 1260)

    def test_handoff_blocks_new_entry_and_commit_but_drains_active_round(self):
        token = rounds.admit(self.root, "0001", owner_token="owner")
        (self.root / "control/HANDOFF").touch()
        with self.assertRaises(ValueError):
            rounds.commit(self.root, "0001", token, owner_token="owner")
        with self.assertRaises(ValueError):
            rounds.admit(self.root, "0001", owner_token="owner")
        (self.root / "control/HANDOFF").unlink()
        self.bind()
        (self.root / "control/HANDOFF").touch()
        self.assertIsNotNone(rounds.active_round(self.root))
        self.assertEqual(
            rounds.admit(self.root, "0001", owner_token="owner"),
            rounds.active_round(self.root)["token"],
        )

    def test_committed_round_cannot_rebind_changed_identity_before_cutoff(self):
        token = self.bind()
        self.write(
            "control/continuation.json", self.lease | {"authorization": "changed"}
        )
        with self.assertRaisesRegex(ValueError, "cannot be rebound"):
            rounds.admit(self.root, "0001", owner_token="owner")
        self.assertEqual(
            json.loads((self.root / "control/round-admissions/0001.json").read_text())[
                "token"
            ],
            token,
        )

    def test_expired_formal_window_keeps_recovery_process_accounting_timeout(self):
        self.bind()
        self.clock[0] = 1200
        rounds.execution_deadline(self.root, 1000, 60, attempt="formal/candidate")
        self.clock[0] = 1300
        backend = ProTokBackend(Path("/canonical"), self.root / "control", "owner")
        prepared = {"fingerprint": "candidate"}
        process = Mock()
        process.returncode = 1
        process.poll.side_effect = [None, 1]
        with (
            patch.object(backend, "_gate"),
            patch.object(backend, "prepare_config", return_value=prepared),
            patch.object(
                backend,
                "_config",
                return_value={"records_root": str(self.root / "records")},
            ),
            patch.object(backend, "_command", return_value=["fixed-evaluator"]),
            patch(
                "integrations.protok.backend.subprocess.Popen", return_value=process
            ) as launch,
        ):
            with self.assertRaises(ProTokError):
                backend.evaluate(
                    self.root / "source", self.root / "config.json", 240, recover=True
                )
        self.assertEqual(
            launch.call_args.args[0][-2:], ["--execution-deadline", "1260"]
        )
        self.assertGreater(process.wait.call_args.kwargs["timeout"], 0.4)
        self.assertFalse(process.send_signal.called)

    def test_prepared_record_and_metadata_do_not_commit_entry(self):
        token = rounds.admit(self.root, "0001", owner_token="owner")
        self.write("study/iterations/0001/iteration.json", {"round_admission": token})
        self.write(
            "study/state.json", {"iteration": 0, "phase": "done", "status": "ready"}
        )
        self.clock[0] = 1000
        self.assertIsNone(rounds.active_round(self.root))
        with self.assertRaises(ValueError):
            rounds.admit(self.root, "0001", owner_token="owner")

    def test_same_process_crossing_cutoff_before_commit_never_enters(self):
        initialize(
            self.root / "study", goal="Observe a bounded round", max_iterations=None
        )
        delegate = RecordedHarness()

        def prepare(iteration):
            token = rounds.admit(self.root, iteration, owner_token="owner")
            self.clock[0] = 1000
            return token

        delegate.admit_iteration = prepare
        delegate.commit_iteration = lambda iteration, token: rounds.commit(
            self.root, iteration, token, owner_token="owner"
        )
        study = Study(self.root / "study", delegate)
        with self.assertRaisesRegex(ValueError, "before commit"):
            study.run()
        self.assertEqual(study.status()["iteration"], 0)
        self.assertEqual(study.status()["phase"], "done")
        self.assertFalse(delegate.calls)
        self.assertIsNone(rounds.active_round(self.root))

    def test_missing_state_commit_or_wrong_iteration_never_authorizes(self):
        self.bind()
        self.write(
            "study/state.json", {"iteration": 1, "phase": "execute", "status": "ready"}
        )
        self.clock[0] = 1200
        self.assertIsNone(rounds.active_round(self.root))
        with self.assertRaises(ProTokError):
            admission(self.root, self.backend)

    def test_stop_and_any_control_change_revoke_active_round(self):
        self.bind()
        self.clock[0] = 1200
        for path, original in (
            ("control/ownership.json", self.owner),
            ("control/continuation.json", self.lease),
        ):
            self.write(path, original | {"identity_change": True})
            self.assertIsNone(rounds.active_round(self.root))
            with self.assertRaises(ProTokError):
                admission(self.root, self.backend)
            self.write(path, original)
        (self.root / "control/STOP").touch()
        self.assertIsNone(rounds.active_round(self.root))
        with self.assertRaises(ProTokError):
            admission(self.root, self.backend)

    def test_review_may_run_after_cutoff_with_same_role_timeout(self):
        self.bind(phase="review")
        self.clock[0] = 1200
        delegate = Mock()
        harness = ProTokHarness(self.backend, self.root, delegate, 1000)
        harness.run(prompt="review", writable=False, timeout_seconds=37)
        self.assertEqual(delegate.run.call_args.kwargs["timeout_seconds"], 37)
        self.assertEqual(harness.call_allowance(-100), 3600)

    def test_real_core_finishes_review_then_blocks_next_round(self):
        initialize(
            self.root / "study",
            goal="Observe a bounded round",
            max_seconds=1,
            max_iterations=None,
            harness=HarnessConfig(timeout_seconds=60),
        )
        delegate = RecordedHarness()
        delegate.stop_after = 10
        delegate.admit_iteration = lambda iteration: rounds.admit(
            self.root, iteration, owner_token="owner"
        )
        delegate.commit_iteration = lambda iteration, token: rounds.commit(
            self.root, iteration, token, owner_token="owner"
        )
        delegate.call_allowance = lambda remaining: (
            60 if rounds.active_round(self.root) else remaining
        )
        study = Study(self.root / "study", delegate)
        original = delegate.response

        def response(phase, context, kwargs):
            if phase == "plan":
                self.clock[0] = 1200
                # Force exhaustion of the old derived total wall budget.
                study.config["max_seconds"] = 0
            self.assertLessEqual(kwargs["timeout_seconds"], 60)
            return original(phase, context, kwargs)

        delegate.response = response
        state = study.run()
        self.assertEqual((state["iteration"], state["phase"]), (1, "done"))
        self.assertEqual(delegate.count("review"), 1)
        self.assertTrue((self.root / "study/iterations/0001/publication.json").exists())
        self.assertIsNone(rounds.active_round(self.root))
        self.assertEqual(
            json.loads((self.root / "study/study.json").read_text())["max_seconds"], 1
        )
        with self.assertRaisesRegex(ValueError, "cutoff"):
            study.run()
        self.assertEqual(study.status()["iteration"], 1)
        self.assertEqual(delegate.count("review"), 1)


if __name__ == "__main__":
    unittest.main()
