"""One nonblocking owner-thread pass over finite backend operations.

Only durable dispatch registration precedes invocation. This component never
waits for a callback, submits twice, or turns missing evidence into job absence.
"""

import math

from .dispatch import RuntimeEvents
from .files import publish_artifact
from .operations import Operations
from .store import (
    BudgetExceeded,
    ConflictError,
    InvalidTransition,
    _decode,
    _text,
)
from .updates import _same

_CONSUMER = "backend-requests"
_REQUEST_KINDS = {"backend_cancel_requested", "backend_retry_requested"}
_CLEANUP = {"inspection", "cancellation", "collection", "settlement"}


class BackendScheduler:
    def __init__(self, owner, ledger, invoker, settings):
        self.owner, self.ledger, self.invoker = owner, ledger, invoker
        runtime = owner.snapshot()["config"].get("runtime", {})
        for key in ("action_timeout_seconds", "inspection_interval_seconds"):
            value = settings.get(key)
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{key} must be finite and positive")
        retries = settings.get("action_retries")
        if type(retries) is not int or retries < 0:
            raise ValueError("action_retries must be a nonnegative integer")
        if any(
            not _same(settings[key], runtime.get(key))
            for key in (
                "action_timeout_seconds",
                "inspection_interval_seconds",
                "action_retries",
            )
        ):
            raise ConflictError(
                "Scheduler settings differ from immutable runtime configuration"
            )
        if invoker is not None and not _same(invoker.identity, ledger.adapter):
            raise ConflictError(
                "Invoker adapter differs from the frozen dispatch adapter"
            )
        self.timeout = settings["action_timeout_seconds"]
        self.interval = settings["inspection_interval_seconds"]
        self.retries = retries
        self.operations = Operations(owner)
        self.events = RuntimeEvents(owner)

    def _requests(self):
        with self.owner.store._connection(readonly=True) as conn:
            return [
                _decode(row)
                for row in conn.execute(
                    "SELECT e.* FROM runtime_events e LEFT JOIN runtime_event_acks a ON a.event_id=e.id AND a.consumer=? "
                    "WHERE a.event_id IS NULL AND e.kind IN ('backend_cancel_requested','backend_retry_requested') ORDER BY e.id",
                    (_CONSUMER,),
                )
            ]

    def _history(self, operation_id, kind):
        with self.owner.store._connection(readonly=True) as conn:
            return [
                _decode(row)
                for row in conn.execute(
                    "SELECT token,settlement_status,completed_at FROM dispatch_runs WHERE category='backend' AND operation_id=? "
                    "AND action_kind=? AND settled=1 ORDER BY rowid DESC LIMIT ?",
                    (operation_id, kind, self.retries + 2),
                )
            ]

    def _failures(self, history, anchor=None):
        count = 0
        for run in history:
            if run["token"] == anchor or run["settlement_status"] == "recorded":
                break
            count += 1
        return count

    @staticmethod
    def _allowed(op, kind):
        if kind == "prepare":
            return op["state"] == "pending" and op["inconsistency"] is None
        if kind == "submit":
            return (
                op["state"] == "prepared"
                and op["submission_token"] is None
                and op["inconsistency"] is None
            )
        if kind == "collection":
            return (
                op["state"] == "terminal"
                and op["collection_state"] == "pending"
                and op["inconsistency"] is None
            )
        if kind == "settlement":
            return not op["costs_final"] and (
                op["submission_token"] is not None or op["job_id"] is not None
            )
        if kind == "inspection":
            return (
                op["submission_token"] is not None or op["job_id"] is not None
            ) and (
                op["state"] in {"submitting", "unknown", "submitted"}
                or not op["physical_released"]
                or op["external_open"]
            )
        if kind == "cancellation":
            return (
                op["submission_token"] is not None or op["job_id"] is not None
            ) and (not op["physical_released"] or op["external_open"])
        return False

    def _attention(self, opid, kind, reason, run=None):
        token = run["token"] if run else "none"
        with self.owner._transaction() as (conn, _):
            RuntimeEvents.emit(
                conn,
                f"backend-attention:{opid}:{kind}:{token}:{reason}",
                "needs_attention",
                opid,
                payload_ref=run.get("completion_ref") if run else None,
                identity=run.get("completion_identity") if run else None,
                payload={"action": kind, "reason": reason, "dispatch_token": token},
            )

    def _request(self, operation_id, kind, reason, request_id, *, retry=False):
        _text(reason, "request reason")
        _text(request_id, "request ID")
        op = self.operations.get(operation_id)
        producer = "backend-request:" + request_id
        with self.owner.store._connection(readonly=True) as conn:
            previous = _decode(
                conn.execute(
                    "SELECT * FROM runtime_events WHERE producer_key=?", (producer,)
                ).fetchone()
            )
            unresolved = conn.execute(
                "SELECT 1 FROM dispatch_runs WHERE operation_id=? AND settled=0",
                (operation_id,),
            ).fetchone()
        if previous is not None:
            if previous["subject"] != operation_id or not _same(
                {
                    key: previous["payload"][key]
                    for key in ("action", "reason", "retry")
                },
                dict(action=kind, reason=reason, retry=retry),
            ):
                raise ConflictError("Backend request ID reused with different intent")
            return previous
        if kind == "submit" or not self._allowed(op, kind):
            raise InvalidTransition(
                "Requested action cannot reopen the current operation lifecycle"
            )
        history = self._history(operation_id, kind)
        if retry and (
            unresolved is not None
            or not history
            or history[0]["settlement_status"] != "failed"
        ):
            raise InvalidTransition(
                "Explicit retry requires its last finished failed action and no unresolved sender"
            )
        payload = dict(
            action=kind,
            reason=reason,
            retry=retry,
            anchor=history[0]["token"] if history else None,
        )
        with self.owner._transaction() as (conn, _):
            return RuntimeEvents.emit(
                conn,
                producer,
                "backend_retry_requested" if retry else "backend_cancel_requested",
                operation_id,
                payload=payload,
            )

    def request_cancel(self, operation_id, reason, request_id):
        return self._request(operation_id, "cancellation", reason, request_id)

    def retry_action(self, operation_id, kind, reason, request_id):
        if kind not in {
            "prepare",
            "inspection",
            "cancellation",
            "collection",
            "settlement",
        }:
            raise ValueError("Submit is never a retryable backend action")
        return self._request(operation_id, kind, reason, request_id, retry=True)

    def _request_event(self, payload, operation_id, kind):
        event_id = payload.get("request_event_id")
        if event_id is None:
            return None
        with self.owner.store._connection(readonly=True) as conn:
            row = _decode(
                conn.execute(
                    "SELECT * FROM runtime_events WHERE id=?", (event_id,)
                ).fetchone()
            )
        if (
            row is None
            or row["kind"] not in _REQUEST_KINDS
            or row["subject"] != operation_id
            or row["payload"]["action"] != kind
        ):
            raise ConflictError("Action request differs from its durable request event")
        return row

    def _unknown_submission(self, run, reason):
        envelope = self.operations.envelope(
            run["operation_id"],
            dict(outcome="unknown", job_id=None, proof=None, reason=reason),
        )
        return self.operations.record_receipt(
            run["operation_id"],
            run["operation_revision"],
            "submission",
            "controller-unknown:" + run["token"],
            envelope,
        )

    def settle(self, run):
        run = self.ledger.get(run["token"])
        if run["category"] != "backend" or not run["worker_retired"]:
            raise InvalidTransition("Backend settlement requires its retired callback")
        if run["settled"]:
            return run
        kind, opid = run["action_kind"], run["operation_id"]
        request_event = None
        failure = None
        try:
            packet = self.ledger.reader._read_record(
                run["input_ref"], run["input_identity"]
            )
            request_event = self._request_event(packet, opid, kind)
            if run["status"] == "completed":
                result = self.ledger.result(run["token"])
                if not isinstance(result, dict) or set(result) != {"event_key", "data"}:
                    raise ValueError(
                        "Backend callback result requires exact event_key/data"
                    )
                _text(result["event_key"], "backend event key")
                receipt_kind = "submission" if kind == "submit" else kind
                self.operations.record_receipt(
                    opid,
                    run["operation_revision"],
                    receipt_kind,
                    result["event_key"],
                    self.operations.envelope(opid, result["data"]),
                )
            elif kind == "submit":
                if run["status"] == "not_started":
                    data = dict(
                        outcome="not_submitted",
                        job_id=None,
                        proof=dict(
                            basis="sender_retired", reference=run["completion_ref"]
                        ),
                        reason="Invoker positively retired the unstarted submit callback",
                    )
                    self.operations.record_receipt(
                        opid,
                        run["operation_revision"],
                        "submission",
                        "controller-unstarted:" + run["token"],
                        self.operations.envelope(opid, data),
                    )
                else:
                    self._unknown_submission(
                        run,
                        run["error"]
                        or "Submit callback retired with an ambiguous external outcome",
                    )
                    failure = run["error"] or run["status"]
            elif kind == "prepare":
                data = dict(
                    ready=False,
                    reason=run["error"] or "Free prepare callback failed",
                    effective_parameters={},
                )
                self.operations.record_receipt(
                    opid,
                    run["operation_revision"],
                    "prepare",
                    "controller-prepare-failed:" + run["token"],
                    self.operations.envelope(opid, data),
                )
                failure = run["error"] or run["status"]
            else:
                failure = run["error"] or run["status"]
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            if kind == "submit":
                try:
                    self._unknown_submission(
                        run, "Submit result could not be accepted: " + failure
                    )
                except Exception:
                    pass  # Durable intent/holds and original completion remain authoritative.
            self._attention(opid, kind, "receipt_rejected", run)
        status = "failed" if failure else "recorded"
        settled = self.ledger.settle(run["token"], status, error=failure)
        if request_event is not None:
            if failure is None:
                self.events.ack(_CONSUMER, request_event["id"])
            elif (
                self._failures(
                    self._history(opid, kind), request_event["payload"]["anchor"]
                )
                >= self.retries + 1
            ):
                self._attention(opid, kind, "retry_exhausted", settled)
                self.events.ack(_CONSUMER, request_event["id"])
        return settled

    def _due(self, opid, kind, now, request_event):
        history = self._history(opid, kind)
        anchor = request_event["payload"]["anchor"] if request_event else None
        if self._failures(history, anchor) >= self.retries + 1:
            return None, "retry_exhausted", history[0] if history else None
        if history:
            latest = history[0]
            due = latest["completed_at"] + self.interval
            return due, None, latest
        if kind == "inspection":
            source = self._history(opid, "submit")
            if source:
                return source[0]["completed_at"] + self.interval, None, source[0]
        return now, None, None

    def _dispatch(self, op, kind, now, request_event):
        token = self.ledger.action_token()
        submit_token = (
            "submit-" + token.removeprefix("backend-") if kind == "submit" else None
        )
        permit = None
        if kind == "submit":
            permit = {
                **Operations._binding(op),
                "submission_token": submit_token,
                "owner_epoch": self.owner.owner_epoch,
                "deadline": op["deadline"],
                "operation_revision": op["revision"] + 1,
                "request_ref": op["request_ref"],
            }
        packet = dict(
            operation_id=op["id"],
            action=kind,
            operation=op,
            request=self.operations.request(op["id"]),
            permit=permit,
        )
        if request_event is not None:
            packet["request_event_id"] = request_event["id"]
        input_ref = publish_artifact(self.owner.store.root, packet)
        deadline = now + self.timeout
        if kind in {"prepare", "submit"}:
            deadline = min(deadline, op["deadline"])
        registered = self.ledger.register_action(
            op["id"],
            op["revision"],
            kind,
            input_ref,
            self.invoker.identity,
            deadline,
            launch_token=token,
            submission_token=submit_token,
        )
        if not registered["created"]:
            return False
        # STOP may race intent registration. Fence this token instead of invoking
        # a new prepare/submit; a submit intent alone still does not prove absence.
        if kind in {"prepare", "submit"} and self.owner.snapshot()["stop"]["requested"]:
            observation = self.invoker.retire_unlaunched(
                token, dict(kind="backend", input_ref=input_ref, deadline=deadline)
            )
        else:
            try:
                observation = self.invoker.launch(token, "backend", input_ref, deadline)
            except Exception:
                observation = dict(
                    status="unknown",
                    completion_ref=None,
                    proof_ref=None,
                    worker_identity=None,
                )
                self._attention(op["id"], kind, "launch_unknown", registered["run"])
        self.ledger.observe(token, observation)
        return True

    def _retire_unsubmitted(self, op, reason):
        data = dict(
            outcome="not_submitted",
            job_id=None,
            proof=dict(
                basis="no_submission_intent",
                reference="controller-no-intent:" + op["id"],
            ),
            reason=reason,
        )
        self.operations.record_receipt(
            op["id"],
            op["revision"],
            "submission",
            "controller-no-intent:" + op["id"],
            self.operations.envelope(op["id"], data),
        )

    def _unavailable_no_job(self, op):
        data = dict(
            artifacts=[],
            evaluations=[],
            unavailable_reason="Positive no-submission proof: no job or scientific outputs",
        )
        self.operations.record_receipt(
            op["id"],
            op["revision"],
            "collection",
            "controller-no-job-collection:" + op["id"],
            self.operations.envelope(op["id"], data),
        )

    def schedule(self, now):
        if self.invoker is None:
            raise InvalidTransition("Dispatch requires a configured invoker")
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Scheduler time must be finite UTC")
        requests = self._requests()
        by_operation = {}
        for event in requests:
            by_operation.setdefault(event["subject"], []).append(event)
        with self.owner.store._connection(readonly=True) as conn:
            rows = conn.execute(
                "SELECT o.id FROM operations o JOIN operation_groups g ON g.execute_attempt_id=o.execute_attempt_id "
                "JOIN iterations i ON i.id=g.iteration_id JOIN tasks t ON t.id=i.task_id "
                "ORDER BY CASE WHEN t.source='human' THEN 0 ELSE 1 END,t.priority,t.id,o.entry_index"
            ).fetchall()
        result = dict(progress=0, blocked=[], next_wake=None)

        def wake(value):
            if value is not None and value > now:
                result["next_wake"] = (
                    value
                    if result["next_wake"] is None
                    else min(result["next_wake"], value)
                )

        for row in rows:
            op = self.operations.get(row[0])
            opid = op["id"]
            unresolved = [
                run
                for run in self.ledger.unsettled("backend")
                if run["operation_id"] == opid
            ]
            if unresolved:
                reason = (
                    "unknown_worker"
                    if any(
                        not run["worker_retired"]
                        and run["state"] in {"unknown", "intent"}
                        for run in unresolved
                    )
                    else "action_pending"
                )
                result["blocked"].append(dict(operation_id=opid, reason=reason))
                wake(now + self.interval)
                continue
            active_requests = by_operation.get(opid, [])
            for event in active_requests[:]:
                if not self._allowed(op, event["payload"]["action"]):
                    self._attention(
                        opid, event["payload"]["action"], "request_lifecycle_closed"
                    )
                    self.events.ack(_CONSUMER, event["id"])
                    active_requests.remove(event)
                    result["progress"] += 1
            stopped = self.owner.snapshot()["stop"]["requested"]
            if (
                op["submission_token"] is None
                and op["inconsistency"] is None
                and op["state"] in {"pending", "prepared"}
                and (stopped or now >= op["deadline"])
            ):
                self._retire_unsubmitted(
                    op, "STOP or frozen deadline prevented every submit intent"
                )
                result["progress"] += 1
                op = self.operations.get(opid)
            if (
                op["state"] == "terminal"
                and op["terminal_outcome"] == "not_submitted"
                and op["collection_state"] == "pending"
                and op["inconsistency"] is None
            ):
                self._unavailable_no_job(op)
                result["progress"] += 1
                op = self.operations.get(opid)
            candidates = []
            for kind in (
                "cancellation",
                "prepare",
                "submit",
                "collection",
                "inspection",
                "settlement",
            ):
                event = next(
                    (
                        event
                        for event in active_requests
                        if event["payload"]["action"] == kind
                    ),
                    None,
                )
                if kind == "cancellation" and event is None:
                    continue
                if not self._allowed(op, kind):
                    continue
                due, error, last = self._due(opid, kind, now, event)
                if error:
                    result["blocked"].append(
                        dict(operation_id=opid, reason=kind + ":" + error)
                    )
                    full = self.ledger.get(last["token"]) if last else None
                    self._attention(opid, kind, error, full)
                    if event is not None:
                        self.events.ack(_CONSUMER, event["id"])
                    continue
                if due > now:
                    wake(due)
                    continue
                candidates.append((kind, event))
            if op["inconsistency"] is not None:
                result["blocked"].append(dict(operation_id=opid, reason="quarantined"))
            for kind, event in candidates:
                try:
                    if self._dispatch(op, kind, now, event):
                        result["progress"] += 1
                    break
                except BudgetExceeded as error:
                    result["blocked"].append(dict(operation_id=opid, reason=str(error)))
                    if self.ledger.active_count() >= self.ledger.max_slots:
                        wake(now + self.interval)
                except (ConflictError, InvalidTransition, ValueError) as error:
                    result["blocked"].append(
                        dict(operation_id=opid, reason=f"{kind}: {error}")
                    )
                    self._attention(opid, kind, "dispatch_rejected")
        return result
