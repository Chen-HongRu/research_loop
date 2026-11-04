"""Default role inputs assembled from accepted, feedback-permitted state.

Selection remains a caller-supplied policy. This builder does not invoke models,
open denied evaluator receipts or infer measurements from backend narratives.
"""

from .context import ContextPackage, ContextSnapshot, build_context
from .files import publish_artifact
from .operations import Operations
from .planning_state import PlanningLedger
from .publication import Publisher
from .publication_views import build_views, render_research
from .store import ConflictError, _decode
from .updates import Updater


class RuntimeContexts:
    def __init__(self, owner, *, policy=None, policy_ref="runtime-context-v1"):
        self.owner = owner
        self.policy = policy
        self.policy_ref = policy_ref
        self.reader = Updater(owner, validators={})

    def build(self, call):
        record = self.owner._feedback_snapshot(call.to_dict())
        if record["binding"] is not None:
            # Dispatch/recovery always uses the already selected input. A policy
            # change cannot silently reconstruct a reserved call's context.
            return self.reader._load_binding(record)[0]
        retry = record.get("retry")
        if retry is not None and retry["source_context_ref"] is not None:
            return self.retry_review(
                call, retry["source_context_ref"], retry["source_context_identity"]
            )
        if record["planning"] is not None:
            return PlanningLedger(self.owner).context(call, policy=self.policy)
        config = record["control"]["config"]
        knowledge = Publisher(self.owner).snapshot()
        publications = {
            row["id"]: self.reader._read_record(row["payload_ref"], row["identity"])[
                "normalized_output"
            ]
            for row in knowledge["publications"]
        }
        inputs = {
            key: config[key] for key in ("goal", "global_evaluation") if key in config
        }
        inputs.update(
            permissions=config.get("permissions", {}),
            task=record["task"],
            budget={
                key: record["control"][key]
                for key in (
                    "max_iterations",
                    "max_calls",
                    "max_seconds",
                    "admission_deadline",
                )
            },
        )
        if call.role in {"plan", "execute"}:
            inputs.update(
                (key, config[key])
                for key in (
                    "execution_backends",
                    "resource_pools",
                    "operation_authorizations",
                )
                if key in config
            )
        protected = list(inputs)
        references, revisions = {}, {}

        def source(key, value, ref, revision, *, required=False):
            inputs[key] = value
            references[key], revisions[key] = ref, revision
            if required:
                protected.append(key)

        if call.role in {"execute", "review"}:
            plan = record["plan"]
            source(
                "plan",
                self.reader._plan_output(record),
                plan["output_ref"],
                plan["id"],
                required=True,
            )
        if call.role == "review":
            with self.owner.store._connection(readonly=True) as conn:
                execute = _decode(
                    conn.execute(
                        "SELECT id,output_ref,output_identity_json FROM attempts "
                        "WHERE iteration_id=? AND role='execute' AND status='accepted'",
                        (call.iteration_id,),
                    ).fetchone()
                )
            if execute is None:
                raise ConflictError("Review requires its accepted Execute")
            source(
                "execute",
                self.reader._read_record(
                    execute["output_ref"], execute["output_identity"]
                )["normalized_output"],
                execute["output_ref"],
                execute["id"],
                required=True,
            )
            operations = [
                {
                    key: operation[key]
                    for key in (
                        "id",
                        "revision",
                        "backend",
                        "backend_version",
                        "environment",
                        "state",
                        "terminal_outcome",
                        "collection_state",
                        "collection_ref",
                        "inconsistency",
                        "job_id",
                        "unexpected_job_ids",
                    )
                }
                for operation in Operations(self.owner).list(
                    iteration_id=call.iteration_id
                )
            ]
            if operations:
                reference = publish_artifact(
                    self.owner.store.root, {"operations": operations}
                )
                source(
                    "operations",
                    operations,
                    reference,
                    {op["id"]: op["revision"] for op in operations},
                    required=True,
                )

        # Each receipt has its own source field so the evidence allowlist points
        # to the original evaluator artifact, not merely to a rendered summary.
        visible = {row["measurement_id"] for row in knowledge["eligibility"]}
        for measurement in knowledge["measurements"]:
            own_review = (
                call.role == "review"
                and measurement["iteration_id"] == call.iteration_id
            )
            if measurement["id"] in visible or own_review:
                source(
                    f"measurement:{measurement['id']}",
                    measurement,
                    measurement["receipt_ref"],
                    measurement["id"],
                    required=own_review,
                )
        research = {
            "version": knowledge["version"],
            "research": render_research(knowledge, publications),
            "frontiers": build_views(knowledge, config["global_evaluation"])
            if isinstance(config.get("global_evaluation"), dict)
            else None,
        }
        reference = publish_artifact(self.owner.store.root, research)
        source("research", research["research"], reference, knowledge["version"])
        source("frontiers", research["frontiers"], reference, knowledge["version"])
        snapshot = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions={
                "study": 1,
                "task": record["task"]["revision"] if record["task"] else None,
                "iteration": call.expected_revision,
                "knowledge": knowledge["version"],
            },
            inputs=inputs,
            sources=[
                {
                    "id": key,
                    "key": key,
                    "ref": references.get(key, f"control:{key}"),
                    "revision": revisions.get(key, 1),
                    "reason": "Accepted controller input at invocation start",
                    "allow_feedback": True,
                }
                for key in inputs
            ],
            protected_keys=protected,
            allowed_evidence=list(dict.fromkeys(references.values())),
        )
        return build_context(
            snapshot,
            call.role,
            record["task"],
            self.policy_ref,
            config.get("context_budget", {}),
            policy=self.policy,
        )

    def retry_review(self, call, original_context_ref, original_identity):
        """Retain the failed Review's selected scientific inputs on explicit retry."""
        if call.role != "review":
            raise ValueError("Frozen review retry cannot be used for another role")
        original = self.reader._read_record(original_context_ref, original_identity)
        package = ContextPackage.from_dict(original)
        value = package.to_dict()
        record = self.owner._feedback_snapshot(call.to_dict())
        if value["manifest"]["task"] != record["task"]:
            raise ConflictError("Review retry task differs from its original input")
        value["manifest"]["revisions"]["iteration"] = call.expected_revision
        return ContextPackage.from_dict(value)
