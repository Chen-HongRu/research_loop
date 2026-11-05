"""Append accepted Scout/Review knowledge and measurement eligibility.

Scientific records remain immutable artifacts. SQLite stores their identities,
per-target revisions, eligibility decisions and durable publication conflicts.
"""

import ipaddress
import json
from urllib.parse import urlsplit

from .store import ConflictError, InvalidTransition, _decode, _json
from .updates import CallIdentity, _identity, _same

_KNOWLEDGE = {
    "id",
    "expected_revision",
    "kind",
    "statement",
    "conditions",
    "source_refs",
    "measurement_ids",
    "interpretation",
    "limitations",
}
_DECISION = {"measurement_id", "valid", "verified", "reason"}
_CORRECTION = _DECISION | {"expected_revision", "evidence_refs"}
_MEASUREMENT_METADATA = "id,iteration_id,receipt_ref,identity_json"


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonblank text")


def _strings(value, field, *, nonempty=False, unique=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{field} must be an explicit list")
    for item in value:
        _text(item, field)
    if unique and len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicates")


def _revision(value, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError("Expected revision must be an integer in the allowed range")


def _public_url(value):
    if any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        parsed.port  # Reject malformed ports; no DNS lookup or remote request.
        host = parsed.hostname.lower().rstrip(".")
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return (
                "." in host
                and host != "localhost"
                and not host.endswith((".localhost", ".local"))
            )
    except ValueError:
        return False


class Publisher:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def _root(self):
        return self.coordinator.store.root

    def _check_ref(self, reference, identity):
        if not _same(_identity(self._root, reference), identity):
            raise ConflictError("Published evidence artifact identity changed")

    def _read_proposal(self, record):
        attempt = record["attempt"]
        self._check_ref(attempt["output_ref"], attempt["output_identity"])
        proposal = json.loads((self._root / attempt["output_ref"]).read_text())
        self._check_ref(attempt["output_ref"], attempt["output_identity"])
        if not _same(
            proposal["call"],
            CallIdentity.from_attempt(record["control"]["study_id"], attempt).to_dict(),
        ):
            raise ConflictError("Accepted proposal identity differs from reserved call")
        if proposal["context_ref"] != record["binding"]["context_ref"]:
            raise ConflictError("Accepted proposal context differs from frozen binding")
        return proposal

    def _accepted(self, call):
        if not isinstance(call, CallIdentity):
            raise TypeError("Expected CallIdentity")
        record = self.coordinator._feedback_snapshot(call.to_dict())
        if record["attempt"]["role"] not in {"scout", "review"}:
            raise InvalidTransition("Only Scout and Review attempts can publish")
        if record["attempt"]["status"] != "accepted" or record["binding"] is None:
            raise InvalidTransition(
                "Publication requires accepted context-bound feedback"
            )
        self._check_ref(record["binding"]["context_ref"], record["binding"]["identity"])
        return record

    def _prior(self, attempt_id):
        with self.coordinator.store._connection(readonly=True) as conn:
            return _decode(
                conn.execute(
                    "SELECT * FROM publications WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
            )

    @staticmethod
    def _decision(entry, *, correction=False):
        expected = _CORRECTION if correction else _DECISION
        if not isinstance(entry, dict) or set(entry) != expected:
            raise ValueError("Invalid measurement eligibility decision fields")
        _text(entry["measurement_id"], "measurement_id")
        _text(entry["reason"], "reason")
        if type(entry["valid"]) is not bool or type(entry["verified"]) is not bool:
            raise ValueError("Eligibility flags must be booleans")
        if correction:
            _revision(entry["expected_revision"], minimum=1)
            _strings(
                entry["evidence_refs"],
                "correction evidence",
                nonempty=True,
                unique=True,
            )

    def _prepare(self, record, proposal):
        payload = proposal["normalized_output"]
        if (
            not isinstance(payload, dict)
            or set(payload) != {"knowledge", "reviews", "corrections"}
            or any(not isinstance(value, list) for value in payload.values())
        ):
            raise ValueError(
                "Publication requires exact knowledge/reviews/corrections lists"
            )
        role, iteration_id = (
            record["attempt"]["role"],
            record["attempt"]["iteration_id"],
        )
        if role == "scout" and (payload["reviews"] or payload["corrections"]):
            raise ValueError("Scout cannot originate measurement eligibility decisions")
        used_refs, measurements = {}, {}
        permitted = set(proposal["evidence_refs"])
        frozen = record["binding"]["evidence_identities"]

        def require_ref(reference):
            if reference not in permitted or reference not in frozen:
                raise ValueError("Publication cites evidence outside accepted feedback")
            identity = frozen[reference]
            if not _same(proposal["evidence_identities"].get(reference), identity):
                raise ConflictError(
                    "Accepted evidence differs from binding-time identity"
                )
            used_refs[reference] = identity

        with self.coordinator.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            own = (
                [
                    _decode(row)
                    for row in conn.execute(
                        f"SELECT {_MEASUREMENT_METADATA} FROM measurements WHERE iteration_id=? ORDER BY id",
                        (iteration_id,),
                    )
                ]
                if role == "review"
                else []
            )
            own_ids = {row["id"] for row in own}

            def measurement(identity):
                if identity not in measurements:
                    row = _decode(
                        conn.execute(
                            f"SELECT {_MEASUREMENT_METADATA} FROM measurements WHERE id=?",
                            (identity,),
                        ).fetchone()
                    )
                    if row is None:
                        raise ValueError(
                            "Publication cites an unregistered measurement"
                        )
                    published = (
                        conn.execute(
                            "SELECT 1 FROM eligibility WHERE measurement_id=? LIMIT 1",
                            (identity,),
                        ).fetchone()
                        is not None
                    )
                    if not published and identity not in own_ids:
                        raise ValueError(
                            "Historical measurement has no published eligibility"
                        )
                    require_ref(row["receipt_ref"])
                    if not _same(frozen[row["receipt_ref"]], row["identity"]):
                        raise ConflictError(
                            "Measurement registry receipt identity differs"
                        )
                    measurements[identity] = row
                return measurements[identity]

            reviewed = []
            for entry in payload["reviews"]:
                self._decision(entry)
                if entry["measurement_id"] not in own_ids:
                    raise ValueError(
                        "Review can only initially qualify its own measurements"
                    )
                measurement(entry["measurement_id"])
                reviewed.append(entry["measurement_id"])
            if len(reviewed) != len(set(reviewed)) or set(reviewed) != own_ids:
                raise ValueError(
                    "Review must qualify every own measurement exactly once"
                )
            for entry in payload["corrections"]:
                self._decision(entry, correction=True)
                target = measurement(entry["measurement_id"])
                if (
                    conn.execute(
                        "SELECT 1 FROM eligibility WHERE measurement_id=? LIMIT 1",
                        (target["id"],),
                    ).fetchone()
                    is None
                ):
                    raise ValueError(
                        "Correction requires previously published eligibility"
                    )
                if target["receipt_ref"] not in entry["evidence_refs"]:
                    raise ValueError(
                        "Correction must include the original measurement receipt"
                    )
                for reference in entry["evidence_refs"]:
                    require_ref(reference)
            for entry in payload["knowledge"]:
                if not isinstance(entry, dict) or set(entry) != _KNOWLEDGE:
                    raise ValueError("Invalid knowledge entry fields")
                _text(entry["id"], "knowledge id")
                _revision(entry["expected_revision"])
                if entry["kind"] not in {
                    "external",
                    "local_measurement",
                    "interpretation",
                }:
                    raise ValueError("Unknown knowledge kind")
                _text(entry["statement"], "statement")
                if not isinstance(entry["interpretation"], str):
                    raise ValueError("interpretation must be explicit text")
                _strings(entry["conditions"], "conditions", nonempty=True)
                _strings(entry["limitations"], "limitations", nonempty=True)
                _strings(entry["source_refs"], "source_refs", unique=True)
                _strings(entry["measurement_ids"], "measurement_ids", unique=True)
                if entry["kind"] == "external" and not entry["source_refs"]:
                    raise ValueError("External knowledge requires explicit sources")
                if entry["kind"] == "local_measurement":
                    if role != "review" or not entry["measurement_ids"]:
                        raise ValueError(
                            "Local measurement knowledge requires Review registry evidence"
                        )
                for reference in entry["source_refs"]:
                    if not _public_url(reference):
                        require_ref(reference)
                for identity in entry["measurement_ids"]:
                    measurement(identity)
        for reference, identity in used_refs.items():
            self._check_ref(reference, identity)
        return payload, own, measurements, used_refs

    def _check_current(self, conn, call, captured, own, measurements, used_refs):
        current = self.coordinator._feedback_record(
            conn, call.to_dict(), metadata_only=True
        )
        if not _same(current["attempt"], captured["attempt"]) or not _same(
            current["binding"], captured["binding"]
        ):
            raise ConflictError("Accepted feedback or context binding changed")
        binding, attempt = current["binding"], current["attempt"]
        self._check_ref(binding["context_ref"], binding["identity"])
        self._check_ref(attempt["output_ref"], attempt["output_identity"])
        if call.role == "review":
            now_own = [
                _decode(row)
                for row in conn.execute(
                    f"SELECT {_MEASUREMENT_METADATA} FROM measurements WHERE iteration_id=? ORDER BY id",
                    (call.iteration_id,),
                )
            ]
            if not _same(now_own, own):
                raise ConflictError("Current Review measurement registry changed")
        for identity, expected in measurements.items():
            row = _decode(
                conn.execute(
                    f"SELECT {_MEASUREMENT_METADATA} FROM measurements WHERE id=?",
                    (identity,),
                ).fetchone()
            )
            if not _same(row, expected):
                raise ConflictError("Measurement registry identity changed")
        for reference, identity in used_refs.items():
            self._check_ref(reference, identity)
        return current

    @staticmethod
    def _conflict(
        conn, publication_id, target_kind, target_id, expected, actual, reason, index
    ):
        cursor = conn.execute(
            "INSERT INTO publication_conflicts(publication_id,target_kind,target_id,expected_revision,actual_revision,reason,entry_index) VALUES(?,?,?,?,?,?,?)",
            (publication_id, target_kind, target_id, expected, actual, reason, index),
        )
        return dict(
            id=cursor.lastrowid,
            target_kind=target_kind,
            target_id=target_id,
            expected_revision=expected,
            actual_revision=actual,
            reason=reason,
            entry_index=index,
        )

    @staticmethod
    def _predecessor_conflict(expected, actual, visible, knowledge_version):
        if expected != visible:
            return (
                "snapshot_revision_mismatch: "
                f"knowledge version {knowledge_version} exposed revision {visible}, "
                f"not expected revision {expected}"
            )
        if expected != actual:
            return "revision_mismatch"
        return None

    def _eligibility(
        self, conn, publication_id, kind, entries, receipt, *, knowledge_version
    ):
        for index, entry in enumerate(entries):
            target = entry["measurement_id"]
            expected = 0 if kind == "reviews" else entry["expected_revision"]
            row = conn.execute(
                "SELECT COALESCE(MAX(revision),0) AS actual, "
                "COALESCE(MAX(CASE WHEN publication_id<=? THEN revision END),0) AS visible "
                "FROM eligibility WHERE measurement_id=?",
                (knowledge_version, target),
            ).fetchone()
            actual = row["actual"]
            reason = self._predecessor_conflict(
                expected, actual, row["visible"], knowledge_version
            )
            if reason is not None:
                receipt["conflicts"].append(
                    self._conflict(
                        conn,
                        publication_id,
                        kind,
                        target,
                        expected,
                        actual,
                        reason,
                        index,
                    )
                )
                continue
            conn.execute(
                "INSERT INTO eligibility VALUES(?,?,?,?,?,?,?)",
                (
                    target,
                    actual + 1,
                    int(entry["valid"]),
                    int(entry["verified"]),
                    publication_id,
                    kind,
                    index,
                ),
            )
            receipt["eligibility"].append(
                dict(measurement_id=target, revision=actual + 1)
            )

    def publish(self, call_identity):
        captured = self._accepted(call_identity)
        prior = self._prior(call_identity.attempt_id)
        if prior is not None:
            # Historical publication is independent of later target revisions.
            self._check_ref(prior["payload_ref"], prior["identity"])
            proposal = self._read_proposal(captured)
            frozen = captured["binding"]["evidence_identities"]
            for reference in proposal["evidence_refs"]:
                identity = proposal["evidence_identities"][reference]
                if not _same(frozen.get(reference), identity):
                    raise ConflictError(
                        "Accepted evidence differs from bound source identity"
                    )
                self._check_ref(reference, identity)
            return prior["receipt"]
        proposal = self._read_proposal(captured)
        payload, own, measurements, used_refs = self._prepare(captured, proposal)
        with self.coordinator._transaction() as (conn, _):
            current = self._check_current(
                conn, call_identity, captured, own, measurements, used_refs
            )
            existing = _decode(
                conn.execute(
                    "SELECT * FROM publications WHERE attempt_id=?",
                    (call_identity.attempt_id,),
                ).fetchone()
            )
            if existing is not None:
                return existing["receipt"]
            if call_identity.role == "review":
                iteration = current["iteration"]
                if (
                    iteration is None
                    or iteration["status"] != "active"
                    or iteration["phase"] != "publication"
                    or iteration["revision"] != call_identity.expected_revision + 1
                ):
                    raise InvalidTransition(
                        "Review iteration is not awaiting publication"
                    )
            attempt = current["attempt"]
            cursor = conn.execute(
                "INSERT INTO publications(attempt_id,payload_ref,identity_json,receipt_json) VALUES(?,?,?,?)",
                (
                    call_identity.attempt_id,
                    attempt["output_ref"],
                    _json(attempt["output_identity"]),
                    "{}",
                ),
            )
            publication_id = cursor.lastrowid
            receipt = dict(
                publication_id=publication_id,
                attempt_id=call_identity.attempt_id,
                payload_ref=attempt["output_ref"],
                role=call_identity.role,
                knowledge=[],
                eligibility=[],
                conflicts=[],
            )
            knowledge_version = captured["binding"]["revisions"]["knowledge"]
            self._eligibility(
                conn,
                publication_id,
                "reviews",
                payload["reviews"],
                receipt,
                knowledge_version=knowledge_version,
            )
            self._eligibility(
                conn,
                publication_id,
                "corrections",
                payload["corrections"],
                receipt,
                knowledge_version=knowledge_version,
            )
            for index, entry in enumerate(payload["knowledge"]):
                row = conn.execute(
                    "SELECT revision,kind FROM knowledge WHERE id=? ORDER BY revision DESC LIMIT 1",
                    (entry["id"],),
                ).fetchone()
                actual = row["revision"] if row is not None else 0
                visible = conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM knowledge "
                    "WHERE id=? AND publication_id<=?",
                    (entry["id"], knowledge_version),
                ).fetchone()[0]
                reason = self._predecessor_conflict(
                    entry["expected_revision"], actual, visible, knowledge_version
                )
                if reason is None and row is not None and row["kind"] != entry["kind"]:
                    reason = "kind_change"
                own_ids = {measurement["id"] for measurement in own}
                for measurement_id in entry["measurement_ids"]:
                    if reason is not None or measurement_id in own_ids:
                        continue
                    if (
                        conn.execute(
                            "SELECT 1 FROM eligibility WHERE measurement_id=? "
                            "AND publication_id<=? LIMIT 1",
                            (measurement_id, knowledge_version),
                        ).fetchone()
                        is None
                    ):
                        reason = (
                            "snapshot_measurement_unavailable: "
                            f"measurement {measurement_id} had no published eligibility "
                            f"at knowledge version {knowledge_version}"
                        )
                if reason is not None:
                    receipt["conflicts"].append(
                        self._conflict(
                            conn,
                            publication_id,
                            "knowledge",
                            entry["id"],
                            entry["expected_revision"],
                            actual,
                            reason,
                            index,
                        )
                    )
                    continue
                conn.execute(
                    "INSERT INTO knowledge VALUES(?,?,?,?,?,?)",
                    (
                        entry["id"],
                        actual + 1,
                        entry["kind"],
                        call_identity.role,
                        publication_id,
                        index,
                    ),
                )
                receipt["knowledge"].append(dict(id=entry["id"], revision=actual + 1))
            if call_identity.role == "review":
                conn.execute(
                    "UPDATE iterations SET status='published',phase='done',revision=revision+1 WHERE id=?",
                    (call_identity.iteration_id,),
                )
                conn.execute("UPDATE control SET pool_version=pool_version+1")
            conn.execute(
                "UPDATE publications SET receipt_json=? WHERE id=?",
                (_json(receipt), publication_id),
            )
            self.coordinator._planning_event(
                conn,
                f"publication:{publication_id}",
                "knowledge",
                {
                    "publication_id": publication_id,
                    "role": call_identity.role,
                    "iteration_id": call_identity.iteration_id,
                },
            )
            from .dispatch import RuntimeEvents

            RuntimeEvents.emit(
                conn,
                f"publication:{publication_id}",
                "knowledge_published",
                call_identity.iteration_id or call_identity.attempt_id,
                payload_ref=attempt["output_ref"],
                identity=attempt["output_identity"],
                payload={"publication_id": publication_id, "role": call_identity.role},
            )
            return receipt

    def snapshot(self, *, history=True, iteration_id=None):
        """Return consistent detached metadata; no payload reads or writes."""
        self.coordinator._live()
        with self.coordinator.store._connection(readonly=True) as conn:
            conn.execute("BEGIN")
            result = {
                "version": conn.execute(
                    "SELECT COALESCE(MAX(id),0) FROM publications"
                ).fetchone()[0]
            }
            for key, table in (
                ("publications", "publications"),
                ("knowledge", "knowledge"),
                ("eligibility", "eligibility"),
                ("conflicts", "publication_conflicts"),
                ("measurements", "measurements"),
            ):
                if not history:
                    result[key] = (
                        [
                            _decode(row)
                            for row in conn.execute(
                                "SELECT * FROM measurements WHERE iteration_id=? ORDER BY rowid",
                                (iteration_id,),
                            )
                        ]
                        if key == "measurements" and iteration_id is not None
                        else []
                    )
                    continue
                result[key] = [
                    _decode(row)
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
            return result
