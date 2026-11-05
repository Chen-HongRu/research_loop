"""Actual SQLite request admission and explicit citations without model calls."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from research_loop.runtime.model_requests import (
    call_trace,
    check_active,
    register_trace,
    reserve_request,
)
from research_loop.runtime.store import (
    BudgetExceeded,
    ConflictError,
    OwnershipError,
    StateStore,
)


def _reserve(capability, ordinal):
    try:
        reserve_request(capability, ordinal)
        return "admitted"
    except BudgetExceeded:
        return "budget"


class ModelRequestTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def capability(self, owner, request):
        attempt = owner.begin_attempt("scout", request_id=request)
        return {
            "study_root": str(self.root),
            "attempt_id": attempt["id"],
            "worker_token": attempt["worker_token"],
            "owner_epoch": attempt["owner_epoch"],
        }

    def test_first_request_uses_existing_reservation_and_replay_is_rejected(self):
        store = StateStore.create(self.root, config={}, max_calls=2)
        with store.coordinator() as owner:
            capability = self.capability(owner, "one")
            reserve_request(capability, 1)
            self.assertEqual(store.snapshot()["counters"]["calls"], 1)
            reserve_request(capability, 2)
            self.assertEqual(store.snapshot()["counters"]["calls"], 2)
            with self.assertRaises(ConflictError):
                reserve_request(capability, 2)
            with self.assertRaises(BudgetExceeded):
                reserve_request(capability, 3)
            self.assertEqual(store.snapshot()["counters"]["calls"], 2)

    def test_cross_process_admission_cannot_overspend_last_call(self):
        store = StateStore.create(self.root, config={}, max_calls=3)
        with store.coordinator() as owner:
            capabilities = [self.capability(owner, request) for request in ("a", "b")]
            for capability in capabilities:
                reserve_request(capability, 1)
            with ProcessPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(_reserve, capabilities, (2, 2)))
            self.assertCountEqual(outcomes, ["admitted", "budget"])
            self.assertEqual(store.snapshot()["counters"]["calls"], 3)

    def test_stop_timeout_and_owner_fence_apply_to_followup_requests(self):
        store = StateStore.create(self.root, config={})
        with store.coordinator() as owner:
            capability = self.capability(owner, "one")
            reserve_request(capability, 1)
            with self.assertRaises(BudgetExceeded):
                reserve_request({**capability, "deadline": time.time() - 1}, 2)
            with self.assertRaises(OwnershipError):
                reserve_request({**capability, "worker_token": "forged"}, 2)
            register_trace(capability, self.root / "private-call")
            self.assertEqual(
                call_trace(self.root, capability["attempt_id"]),
                str((self.root / "private-call").resolve()),
            )
            owner.set_stop("stop test")
            with self.assertRaises(BudgetExceeded):
                check_active(capability)
            with self.assertRaises(BudgetExceeded):
                reserve_request(capability, 2)
        with self.assertRaises(OwnershipError):
            reserve_request(capability, 2)

    def test_explicit_fixed_citations_round_trip_without_recording_reads(self):
        from research_loop.runtime.model_requests import (
            format_decision_reference,
            parse_decision_reference,
        )

        ref = dict(library_id="synthetic", kind="knowledge", id="K1", revision=2)
        self.assertEqual(parse_decision_reference(format_decision_reference(ref)), ref)
        for text in (
            "untyped",
            "kb:x:source:y@1",
            "kb:x:knowledge:K1@0",
            "kb:x:knowledge:K1@latest",
        ):
            with self.assertRaises(ValueError):
                parse_decision_reference(text)
