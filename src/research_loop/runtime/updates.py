"""Validate worker proposals against frozen context, then accept atomically.

Validators are trusted, explicitly registered role contracts. Their acceptance is
schema/contract validation, not scientific proof. They never run in a transaction.
"""

from dataclasses import dataclass
import json
from typing import Callable, Mapping

from .context import ContextPackage
from .role_access import RoleAccess
from .context_sources import (
    check_metadata as check_source_metadata,
    validate_package as validate_source_package,
)
from .files import publish_artifact, validate_artifact_ref
from .store import ConflictError, InvalidTransition, _decode, _json


@dataclass(frozen=True)
class CallIdentity:
    study_id: str
    attempt_id: str
    worker_token: str
    owner_epoch: int
    role: str
    iteration_id: str | None
    expected_revision: int | None

    @classmethod
    def from_attempt(cls, study_id, attempt):
        return cls(
            study_id=study_id,
            attempt_id=attempt["id"],
            **{
                key: attempt[key]
                for key in (
                    "worker_token",
                    "owner_epoch",
                    "role",
                    "iteration_id",
                    "expected_revision",
                )
            },
        )

    @classmethod
    def from_dict(cls, value):
        return cls(**value)

    def to_dict(self):
        return dict(
            study_id=self.study_id,
            attempt_id=self.attempt_id,
            worker_token=self.worker_token,
            owner_epoch=self.owner_epoch,
            role=self.role,
            iteration_id=self.iteration_id,
            expected_revision=self.expected_revision,
        )


@dataclass(frozen=True, init=False)
class _FrozenRecord:
    _serialized: str

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_serialized", _json(value))
        return instance

    def to_dict(self):
        return json.loads(self._serialized)


@dataclass(frozen=True, init=False)
class UpdateProposal(_FrozenRecord):
    @property
    def call(self):
        return CallIdentity.from_dict(self.to_dict()["call"])

    @property
    def context_ref(self):
        return self.to_dict()["context_ref"]

    @property
    def normalized_output(self):
        return self.to_dict()["normalized_output"]

    @property
    def evidence_refs(self):
        return self.to_dict()["evidence_refs"]

    @property
    def revisions(self):
        return self.to_dict()["revisions"]


@dataclass(frozen=True, init=False)
class UpdateReceipt(_FrozenRecord):
    @property
    def output_ref(self):
        return self.to_dict()["output_ref"]

    @property
    def revision(self):
        return self.to_dict()["revision"]


_FORBIDDEN = {
    "state_after",
    "authorization",
    "permissions",
    "limits",
    "counters",
    "owner_epoch",
    "stop",
    "config",
}


def _identity(root, reference):
    metadata = validate_artifact_ref(root, reference).stat()
    return dict(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _same(left, right):
    return _json(left) == _json(right)


def _package_text(package):
    # Prompt field order is part of the package identity, unlike object key
    # order in a normalized scientific result.
    return json.dumps(package.to_dict(), ensure_ascii=False, allow_nan=False)


class Updater:
    def __init__(self, coordinator, *, validators: Mapping[str, Callable]):
        self.coordinator = coordinator
        self.validators = dict(validators)
        if any(not callable(validator) for validator in self.validators.values()):
            raise ValueError("Each registered role validator must be callable")

    @property
    def _root(self):
        return self.coordinator.store.root

    @staticmethod
    def _revisions(record):
        if record["binding"] is not None:
            return record["binding"]["revisions"]
        task = record["task"]
        return dict(
            study=1,
            task=task["revision"] if task else None,
            iteration=record["attempt"]["expected_revision"],
            knowledge=record["knowledge_version"],
        )

    def _check_identity(self, reference, expected):
        if not _same(_identity(self._root, reference), expected):
            raise ConflictError("Immutable controller artifact identity changed")

    def _read_record(self, reference, identity):
        """Retrieve one actual control input; never call within a transaction."""
        self._check_identity(reference, identity)
        value = json.loads((self._root / reference).read_text())
        self._check_identity(reference, identity)
        return value

    def _plan_output(self, record):
        if not RoleAccess.resolve(
            record["control"]["config"], record["attempt"]["role"]
        ).accepted_plan:
            return None
        plan = record["plan"]
        if plan is None:
            raise InvalidTransition(
                "Execute/Review requires an accepted validated Plan"
            )
        return self._phase_output(plan, "plan")

    def _phase_output(self, binding, role):
        """Read an accepted native result or an explicitly translated legacy phase."""
        proposal = self._read_record(binding["output_ref"], binding["output_identity"])
        if binding.get("origin_kind") == "legacy":
            origin = proposal.get("origin")
            if (
                proposal.get("kind") != "legacy_phase_binding"
                or not isinstance(origin, dict)
                or origin.get("role") != role
                or not origin.get("source_refs")
                or not origin.get("translator")
            ):
                raise ConflictError("Imported phase lacks its explicit source binding")
        return proposal["normalized_output"]

    def _planning_inputs(self, record):
        planning = record.get("planning")
        if planning is None:
            return {}
        inputs = {
            "planning": self._read_record(
                planning["snapshot_ref"], planning["identity"]
            )
        }
        for advocate in planning.get("advocates", []):
            value = {"attempt_id": advocate["id"], "status": advocate["status"]}
            if advocate["status"] == "accepted":
                value["output"] = self._read_record(
                    advocate["output_ref"], advocate["output_identity"]
                )["normalized_output"]
            elif advocate["status"] == "failed":
                value["failure_reason"] = advocate["failure_reason"]
            else:
                raise InvalidTransition("Judge requires terminal Advocate results")
            inputs[advocate["slot"]] = value
        return inputs

    def _check_package(self, record, package, plan_output):
        context, manifest = package.context, package.manifest
        control, attempt = record["control"], record["attempt"]
        if (
            manifest["study_id"] != control["study_id"]
            or manifest["role"] != attempt["role"]
        ):
            raise ConflictError("Context study or role does not match reservation")
        revisions = self._revisions(record)
        if record["binding"] is None:
            # Independent append-only publication may advance after capture.
            # Corrections use per-target revisions in the Publisher instead.
            knowledge = manifest["revisions"].get("knowledge")
            if (
                type(knowledge) is not int
                or not 0 <= knowledge <= revisions["knowledge"]
            ):
                raise ConflictError("Unknown knowledge snapshot version")
            revisions["knowledge"] = knowledge
        if not _same(manifest["revisions"], revisions):
            raise ConflictError("Context revisions do not match reservation")
        if not _same(manifest["task"], record["task"]):
            raise ConflictError("Context task envelope differs from immutable task")
        config = control["config"]
        access = RoleAccess.resolve(config, attempt["role"])
        validate_source_package(config, attempt["role"], package, self._read_record)
        self._check_research_package(record, package)
        if not _same(manifest["budget"], config.get("context_budget", {})):
            raise ConflictError("Context quota differs from configured quota")
        limits = {
            key: control[key]
            for key in (
                "max_iterations",
                "max_calls",
                "max_seconds",
                "admission_deadline",
            )
        }
        required = dict(
            **access.controls(config),
            task=record["task"],
            budget=limits,
        )
        for key in ("goal", "global_evaluation"):
            if key in config:
                required[key] = config[key]
        if access.resource_context:
            for key in (
                "execution_backends",
                "resource_pools",
                "operation_authorizations",
            ):
                if key in config:
                    required[key] = config[key]
        required.update(self._planning_inputs(record))
        if access.accepted_plan:
            if record["plan"] is None:
                raise InvalidTransition(
                    "Execute/Review requires an accepted validated Plan"
                )
            required["plan"] = plan_output
        if not required.keys() <= set(manifest["protected_keys"]):
            raise ConflictError("Authoritative context inputs must be protected")
        for key, value in required.items():
            if key not in context or not _same(context[key], value):
                raise ConflictError(f"Context {key} differs from authoritative input")
        if access.configured:
            visible_refs = {
                s["ref"] for s in manifest["sources"] if s["disposition"] != "omitted"
            }
            if set(manifest["allowed_evidence"]) - visible_refs:
                raise ConflictError("Role cannot cite omitted context")
            if (
                not access.published_history
                and record["binding"] is None
                and not (record.get("retry") or {}).get("source_context_ref")
            ):
                self._check_iteration_scope(record, package, required, access)
        retry = record.get("retry")
        if retry is not None and retry["source_context_ref"] is not None:
            original = self._read_record(
                retry["source_context_ref"], retry["source_context_identity"]
            )
            original["manifest"]["revisions"]["iteration"] = attempt[
                "expected_revision"
            ]
            if _package_text(ContextPackage.from_dict(original)) != _package_text(
                package
            ):
                raise ConflictError(
                    "Review retry must retain its original frozen inputs"
                )
            for reference, identity in retry["source_evidence_identities"].items():
                self._check_identity(reference, identity)
        if attempt["status"] == "active":
            self.coordinator._iteration_for_feedback(record)
        elif attempt["status"] != "accepted":
            raise InvalidTransition("Attempt no longer accepts feedback")

    def _check_iteration_scope(self, record, package, required, access):
        """Pin evidence to authoritative inputs when Study history is out of scope."""
        from .contexts import own_operations

        ctx, manifest = package.context, package.manifest
        sources = {s["key"]: s for s in manifest["sources"]}
        expected = {key: f"control:{key}" for key in required}
        protected = set(required)
        iteration = record["attempt"]["iteration_id"]
        if access.accepted_plan:
            expected["plan"] = record["plan"]["output_ref"]
        if access.own_execution:
            with self.coordinator.store._connection(readonly=True) as conn:
                execute = _decode(
                    conn.execute(
                        "SELECT * FROM attempts WHERE iteration_id=? AND role='execute' AND status='accepted'",
                        (iteration,),
                    ).fetchone()
                )
                rows = [
                    _decode(r)
                    for r in conn.execute(
                        "SELECT * FROM measurements WHERE iteration_id=?", (iteration,)
                    )
                ]
            if not execute or not _same(
                ctx.get("execute"), self._phase_output(execute, "execute")
            ):
                raise ConflictError("Review requires its own Execute output")
            expected["execute"] = execute["output_ref"]
            protected.add("execute")
            for row in rows:
                key = "measurement:" + row["id"]
                if not _same(ctx.get(key), row):
                    raise ConflictError("Review requires its own measurement")
                expected[key] = row["receipt_ref"]
                protected.add(key)
            operations = own_operations(self.coordinator, iteration)
            if operations:
                ref = sources.get("operations", {}).get("ref")
                if (
                    not _same(ctx.get("operations"), operations)
                    or not ref
                    or not _same(
                        self._read_record(ref, _identity(self._root, ref)),
                        {"operations": operations},
                    )
                ):
                    raise ConflictError("Review operations differ from own iteration")
                expected["operations"] = ref
                protected.add("operations")
        planning = record.get("planning")
        if planning:
            expected["planning"] = planning["snapshot_ref"]
            for slot in planning.get("advocates", []):
                expected[slot["slot"]] = (
                    slot["output_ref"]
                    if slot["status"] == "accepted"
                    else "control:attempt:" + slot["id"]
                )
        # Both namespaces have already undergone independent identity and body checks.
        for key, source in sources.items():
            if key.startswith(("provided:", "evidence:")):
                expected[key] = source["ref"]
        if (
            {key: s["ref"] for key, s in sources.items()} != expected
            or not protected <= set(manifest["protected_keys"])
            or any(
                not ref.startswith("artifacts/") for ref in manifest["allowed_evidence"]
            )
        ):
            raise ConflictError("Context exceeds the role's iteration scope")

    def _check_research_package(self, record, package):
        from .operations import Operations

        attempt = record["attempt"]
        reports, snapshots = (
            Operations(self.coordinator).research_evidence(attempt["iteration_id"])
            if attempt["role"] == "review"
            else ([], [])
        )
        context, manifest = package.context, package.manifest
        actual = {
            source["key"]: source
            for source in manifest["sources"]
            if source["key"].startswith("evidence:")
        }
        expected = {entry["source"]["key"] for entry in reports}
        if (
            set(actual) != expected
            or {key for key in context if key.startswith("evidence:")} - expected
        ):
            raise ConflictError(
                "Research evidence is not from this Review's own collection"
            )
        refs = {entry["source"]["ref"] for entry in reports}
        allowed = {
            entry["source"]["ref"]
            for entry in reports
            if entry["source"]["allow_feedback"]
        }
        if attempt["role"] in record["control"]["config"].get("role_access", {}):
            allowed &= {
                s["ref"] for s in manifest["sources"] if s["disposition"] != "omitted"
            }
        if set(manifest["allowed_evidence"]) & refs != allowed:
            raise ConflictError(
                "Research evidence permissions differ from accepted collection"
            )
        for entry in reports:
            source = entry["source"]
            key = source["key"]
            if not _same({field: actual[key][field] for field in source}, source):
                raise ConflictError(
                    "Research evidence source differs from accepted collection"
                )
            if not source["allow_feedback"]:
                if key in context or actual[key]["disposition"] != "omitted":
                    raise ConflictError(
                        "Denied research report cannot provide feedback"
                    )
                continue
            if key in context:
                body = self._read_record(source["ref"], entry["identity"])
                if (
                    actual[key]["disposition"] != "provided"
                    or not isinstance(body, dict)
                    or not _same(body, context[key])
                ):
                    raise ConflictError(
                        "Research report differs from accepted collection input"
                    )
        record["research_collections"] = snapshots
        record["research_reports"] = reports

    def _load_binding(self, record):
        binding = record["binding"]
        if binding is None:
            raise InvalidTransition("Bind a context before validating worker output")
        package = ContextPackage.from_dict(
            self._read_record(binding["context_ref"], binding["identity"])
        )
        plan_output = self._plan_output(record)
        self._check_package(record, package, plan_output)
        if not _same(binding["revisions"], package.manifest["revisions"]):
            raise ConflictError("Bound context revision metadata differs")
        if set(binding["evidence_identities"]) != set(
            package.manifest["allowed_evidence"]
        ):
            raise ConflictError(
                "Bound evidence identities differ from frozen allowlist"
            )
        return package, plan_output

    def _check_evidence(self, binding, references, claimed_identities=None):
        frozen = binding["evidence_identities"]
        if not set(references) <= frozen.keys():
            raise ValueError("Output references evidence outside its frozen allowlist")
        identities = {reference: frozen[reference] for reference in references}
        if claimed_identities is not None and not _same(identities, claimed_identities):
            raise ConflictError(
                "Proposal evidence differs from binding-time identities"
            )
        for reference, identity in identities.items():
            self._check_identity(reference, identity)
        return identities

    def _check_frozen_refs(self, current, captured):
        """Recheck metadata only, while the caller holds a short transaction."""
        current_task = current["task"]
        captured_task = captured["task"]
        if not _same(
            {key: current_task[key] for key in ("id", "revision")}
            if current_task
            else None,
            {key: captured_task[key] for key in ("id", "revision")}
            if captured_task
            else None,
        ):
            raise ConflictError("Task identity or revision changed")
        if current["attempt"]["status"] == "active":
            self.coordinator._iteration_for_feedback(current)
        if not _same(current["binding"], captured["binding"]):
            raise ConflictError(
                "Context binding changed; retry with its committed reference"
            )
        if current["binding"] is not None:
            binding = current["binding"]
            self._check_identity(binding["context_ref"], binding["identity"])
        if not _same(current["plan"], captured["plan"]):
            raise ConflictError("Accepted Plan reference changed")
        if current["plan"] is not None:
            plan = current["plan"]
            self._check_identity(plan["output_ref"], plan["output_identity"])
        check_source_metadata(
            captured["control"]["config"],
            current["attempt"]["role"],
            self._check_identity,
        )
        if captured.get("research_collections"):
            from .operations import Operations

            with self.coordinator.store._connection(readonly=True) as source_conn:
                for snapshot in captured["research_collections"]:
                    op = Operations._get(source_conn, snapshot["id"])
                    if not _same(Operations.research_snapshot(op), snapshot):
                        raise ConflictError("Accepted research collection changed")
                    self._check_identity(
                        snapshot["collection_ref"], snapshot["collection_identity"]
                    )
            for entry in captured["research_reports"]:
                if entry["source"]["allow_feedback"]:
                    self._check_identity(entry["source"]["ref"], entry["identity"])
        for key in ("dispatch", "reconciliation", "retry"):
            if not _same(current.get(key), captured.get(key)):
                raise ConflictError("Invocation or recovery binding changed")
        dispatch = current.get("dispatch")
        if dispatch is not None:
            self._check_identity(dispatch["input_ref"], dispatch["input_identity"])
            if dispatch["completion_ref"] is not None:
                self._check_identity(
                    dispatch["completion_ref"], dispatch["completion_identity"]
                )
        retry = current.get("retry")
        if retry is not None:
            if retry["source_context_ref"] is not None:
                self._check_identity(
                    retry["source_context_ref"], retry["source_context_identity"]
                )
            self._check_identity(retry["proof_ref"], retry["proof_identity"])
            for reference, identity in retry["source_evidence_identities"].items():
                self._check_identity(reference, identity)
        if not _same(current.get("planning"), captured.get("planning")):
            raise ConflictError("Frozen planning references changed")
        planning = current.get("planning")
        if planning is not None:
            self._check_identity(planning["snapshot_ref"], planning["identity"])
            self._check_identity(
                planning["knowledge_ref"], planning["knowledge_identity"]
            )
            for advocate in planning.get("advocates", []):
                if advocate["status"] == "accepted":
                    self._check_identity(
                        advocate["output_ref"], advocate["output_identity"]
                    )

    def bind_context(self, call_identity, package):
        if not isinstance(call_identity, CallIdentity) or not isinstance(
            package, ContextPackage
        ):
            raise TypeError("Expected CallIdentity and ContextPackage")
        package = ContextPackage.from_dict(package.to_dict())
        call = call_identity.to_dict()
        captured = self.coordinator._feedback_snapshot(call)
        plan_output = self._plan_output(captured)
        self._check_package(captured, package, plan_output)
        existing = captured["binding"]
        if existing is not None:
            original, _ = self._load_binding(captured)
            if _package_text(original) != _package_text(package):
                raise ConflictError("Attempt context is already bound differently")
            reference, identity = existing["context_ref"], existing["identity"]
            evidence_identities = self._check_evidence(
                existing, package.manifest["allowed_evidence"]
            )
        else:
            if captured["attempt"]["status"] != "active":
                raise InvalidTransition("Cannot bind context after acceptance")
            # Freeze source identity when the input capability is granted,
            # before any worker output can cite a replacement under that ref.
            evidence_identities = {
                ref: _identity(self._root, ref)
                for ref in package.manifest["allowed_evidence"]
            }
            reference = publish_artifact(self._root, package.to_dict())
            identity = _identity(self._root, reference)
        revisions_json = _json(package.manifest["revisions"])
        with self.coordinator._transaction() as (conn, _):
            current = self.coordinator._feedback_record(conn, call, metadata_only=True)
            self._check_frozen_refs(current, captured)
            self._check_identity(reference, identity)
            if existing is not None:
                return reference
            if current["attempt"]["status"] != "active":
                raise InvalidTransition("Cannot bind context after acceptance")
            conn.execute(
                "INSERT INTO contexts VALUES(?,?,?,?,?)",
                (
                    call_identity.attempt_id,
                    reference,
                    _json(identity),
                    revisions_json,
                    _json(evidence_identities),
                ),
            )
        return reference

    def _validate(self, call_identity, output, evidence_refs):
        if not isinstance(call_identity, CallIdentity):
            raise TypeError("Expected CallIdentity")
        if not isinstance(output, dict):
            raise ValueError("Worker output must be an object")
        raw = json.loads(_json(output))
        if _FORBIDDEN & raw.keys():
            raise ValueError(
                "Worker output cannot propose control or authorization updates"
            )
        if (
            not isinstance(evidence_refs, list)
            or any(not isinstance(ref, str) for ref in evidence_refs)
            or len(set(evidence_refs)) != len(evidence_refs)
        ):
            raise ValueError("Evidence references must be unique strings")
        captured = self.coordinator._feedback_snapshot(call_identity.to_dict())
        dispatch = captured.get("dispatch")
        if dispatch is not None:
            if not dispatch["worker_retired"] or dispatch["status"] != "completed":
                raise InvalidTransition(
                    "Managed output requires a retired completed invocation"
                )
            completion = self._read_record(
                dispatch["completion_ref"], dispatch["completion_identity"]
            )
            if not _same(
                completion["result"], {"output": raw, "evidence_refs": evidence_refs}
            ):
                raise ConflictError("Worker output differs from its durable completion")
        package, _ = self._load_binding(captured)
        if not set(evidence_refs) <= set(package.manifest["allowed_evidence"]):
            raise ValueError("Output references evidence outside its frozen allowlist")
        identities = self._check_evidence(captured["binding"], evidence_refs)
        validator = self.validators.get(call_identity.role)
        if validator is None:
            raise InvalidTransition("No validator registered for worker role")
        normalized = validator(json.loads(_json(raw)), package.context)
        if not isinstance(normalized, dict):
            raise ValueError("Validator must return a normalized object")
        normalized = json.loads(_json(normalized))
        RoleAccess.resolve(
            captured["control"]["config"], call_identity.role
        ).check_output(normalized)
        if _FORBIDDEN & normalized.keys():
            raise ValueError("Normalized output cannot update control or authorization")
        reports = {
            entry["source"]["ref"] for entry in captured.get("research_reports", [])
        }
        for entry in normalized.get("knowledge", []):
            if (
                isinstance(entry, dict)
                and reports.intersection(entry.get("source_refs", []))
                and entry.get("kind") != "interpretation"
            ):
                raise ValueError(
                    "Unverified research reports may only support interpretation knowledge"
                )
        if call_identity.role == "execute":
            from .operations import Operations

            captured["operation_admission"] = Operations(
                self.coordinator
            ).prepare_execution(
                call_identity,
                normalized,
                self._plan_output(captured),
                captured["control"]["config"],
            )
        proposal = UpdateProposal.from_dict(
            dict(
                call=call_identity.to_dict(),
                context_ref=captured["binding"]["context_ref"],
                output=raw,
                normalized_output=normalized,
                evidence_refs=list(evidence_refs),
                evidence_identities=identities,
                revisions=self._revisions(captured),
            )
        )
        return proposal, captured

    def validate_output(self, call_identity, output, evidence_refs):
        return self._validate(call_identity, output, evidence_refs)[0]

    @staticmethod
    def _expected_revisions(proposed, expected, captured):
        if not _same(expected, proposed["revisions"]) or not _same(
            expected, captured["binding"]["revisions"]
        ):
            raise ConflictError("Expected revisions differ from frozen context")

    def _check_accepted_refs(self, record, proposed):
        binding, attempt = record["binding"], record["attempt"]
        if binding is None or proposed["context_ref"] != binding["context_ref"]:
            raise ConflictError("Proposal context does not match its bound reference")
        self._check_identity(binding["context_ref"], binding["identity"])
        self._check_identity(attempt["output_ref"], attempt["output_identity"])
        self._check_evidence(
            binding, proposed["evidence_refs"], proposed["evidence_identities"]
        )

    def _replay(self, captured, proposed, expected_revisions):
        attempt = captured["attempt"]
        original = self._read_record(attempt["output_ref"], attempt["output_identity"])
        if not _same(original, proposed):
            raise ConflictError("Attempt already accepted a different proposal")
        self._expected_revisions(proposed, expected_revisions, captured)
        self._check_accepted_refs(captured, proposed)
        # A committed receipt is historical evidence. Today's validator may have
        # changed or been removed, and has no authority to rewrite that receipt.
        with self.coordinator._transaction() as (conn, _):
            current = self.coordinator._feedback_record(
                conn, proposed["call"], metadata_only=True
            )
            if not _same(current["attempt"], attempt):
                raise ConflictError("Accepted attempt changed")
            self._check_frozen_refs(current, captured)
            self._check_accepted_refs(current, proposed)
            return UpdateReceipt.from_dict(current["attempt"]["receipt"])

    def apply_update(self, proposal, expected_revisions):
        if not isinstance(proposal, UpdateProposal):
            raise TypeError("Expected UpdateProposal")
        proposed = proposal.to_dict()
        try:
            call = CallIdentity.from_dict(proposed["call"])
            captured = self.coordinator._feedback_snapshot(call.to_dict())
            if captured["attempt"]["status"] == "accepted":
                return self._replay(captured, proposed, expected_revisions)
            validated, captured = self._validate(
                call, proposed["output"], proposed["evidence_refs"]
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("Malformed update proposal") from exc
        if not _same(proposed, validated.to_dict()):
            raise ConflictError(
                "Proposal differs from validated output and bound context"
            )
        self._expected_revisions(proposed, expected_revisions, captured)
        output_ref = publish_artifact(self._root, proposed)
        identity = _identity(self._root, output_ref)
        with self.coordinator._transaction() as (conn, control):
            current = self.coordinator._feedback_record(
                conn, call.to_dict(), metadata_only=True
            )
            self._check_frozen_refs(current, captured)
            if current["attempt"]["status"] != "active":
                raise ConflictError(
                    "Attempt was concurrently accepted; retry its committed receipt"
                )
            self._check_identity(output_ref, identity)
            self._check_evidence(
                current["binding"],
                proposed["evidence_refs"],
                proposed["evidence_identities"],
            )
            extra = dict(
                context_ref=proposed["context_ref"],
                revisions=proposed["revisions"],
                evidence_refs=proposed["evidence_refs"],
            )
            if call.role == "execute":
                from .operations import Operations

                operation_ids = Operations.seal_execution(
                    conn,
                    call,
                    output_ref,
                    identity,
                    captured["operation_admission"],
                    control["anchor"],
                )
                if operation_ids:
                    extra["operation_ids"] = operation_ids
            receipt = self.coordinator._accept_record(
                conn,
                current["attempt"],
                output_ref,
                identity,
                receipt_extra=extra,
            )
            return UpdateReceipt.from_dict(receipt)
