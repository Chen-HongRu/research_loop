"""Synthetic, temporary SQLite publication smoke; no models, network or GPU.

The explicit validators below return fixed fixture contracts. They are neither
production validators nor scientific verification of the synthetic observations.
Run with: uv run python examples/runtime_publication.py
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from research_loop.runtime.context import ContextSnapshot, build_context
from research_loop.runtime.files import publish_artifact
from research_loop.runtime.measurements import MeasurementRegistry
from research_loop.runtime.projections import ProjectionWriter
from research_loop.runtime.publication import Publisher
from research_loop.runtime.store import StateStore
from research_loop.runtime.updates import CallIdentity, Updater


EVALUATION = dict(
    id="synthetic-score",
    comparison_scope="fixed-fixture-v1",
    protocol="Deterministic example values, not a scientific evaluation",
    data_role="development",
    allow_feedback=True,
    objectives=[dict(name="score", direction="maximize", unit="points")],
    constraints=[],
)


def make_store(root):
    return StateStore.create(
        root,
        config=dict(
            goal="Exercise local publication",
            permissions={},
            global_evaluation=EVALUATION,
        ),
    )


def _fixture_validator(output, context):
    """Return the supplied synthetic contract; establish no scientific claim."""
    return json.loads(json.dumps(output))


class SyntheticSession:
    """Small fixture driver shared by this executable example and its checks."""

    def __init__(self, owner):
        self.owner = owner
        self.store = owner.store
        self.root = self.store.root
        self.sequence = 0
        self.updater = Updater(
            owner,
            validators={
                role: _fixture_validator
                for role in ("plan", "execute", "review", "scout")
            },
        )

    def accept(self, role, output, *, iteration=None, refs=()):
        self.sequence += 1
        scope = (
            {}
            if iteration is None
            else dict(
                iteration_id=iteration["id"], expected_revision=iteration["revision"]
            )
        )
        attempt = self.owner.begin_attempt(
            role, request_id=f"synthetic-{self.sequence}", **scope
        )
        call = CallIdentity.from_attempt(self.store.snapshot()["study_id"], attempt)
        record = self.owner._feedback_snapshot(call.to_dict())
        config = self.store.snapshot()["config"]
        inputs = {
            key: config[key] for key in ("goal", "permissions", "global_evaluation")
        }
        if role in {"plan", "execute"}:
            inputs.update(
                (key, config[key])
                for key in (
                    "execution_backends",
                    "resource_pools",
                    "operation_authorizations",
                )
                if key in config
            )
        inputs.update(task=record["task"], budget=self.store.snapshot()["limits"])
        if role in {"execute", "review"}:
            plan = record["plan"]
            inputs["plan"] = self.updater._read_record(
                plan["output_ref"], plan["output_identity"]
            )["normalized_output"]
        protected = list(inputs)
        sources = [
            dict(
                id=key,
                key=key,
                ref=f"control:{key}",
                revision=1,
                reason="Frozen fixture control input",
                allow_feedback=True,
            )
            for key in inputs
        ]
        for index, ref in enumerate(refs):
            key = f"receipt-{index}"
            inputs[key] = "Trusted synthetic evaluator receipt"
            sources.append(
                dict(
                    id=key,
                    key=key,
                    ref=ref,
                    revision=1,
                    reason="Synthetic evaluator input",
                    allow_feedback=True,
                )
            )
        snapshot = ContextSnapshot.capture(
            study_id=call.study_id,
            revisions=dict(
                study=1,
                task=record["task"]["revision"] if record["task"] else None,
                iteration=call.expected_revision,
                knowledge=record["knowledge_version"],
            ),
            inputs=inputs,
            sources=sources,
            protected_keys=protected,
            allowed_evidence=list(refs),
        )
        package = build_context(
            snapshot, role, record["task"], "synthetic-fixture:v1", {}
        )
        self.updater.bind_context(call, package)
        proposal = self.updater.validate_output(call, output, list(refs))
        self.updater.apply_update(proposal, proposal.revisions)
        return call

    def iteration(self, identity):
        return next(
            row for row in self.store.snapshot()["iterations"] if row["id"] == identity
        )

    def trial(self, name, score=None):
        self.owner.create_task(
            name, dict(instruction=f"Synthetic {name}", route_id="fixture")
        )
        iteration = self.owner.claim_task(name)
        self.accept("plan", {"local_evaluation": None}, iteration=iteration)
        artifact = dict(path=f"synthetic/{name}.bin", version="fixture-v1")
        execute = self.accept(
            "execute",
            {"artifacts": [artifact] if score is not None else []},
            iteration=self.iteration(iteration["id"]),
        )
        measurement = None
        if score is not None:
            ref = publish_artifact(
                self.root,
                dict(
                    version=1,
                    iteration_id=iteration["id"],
                    execute_attempt_id=execute.attempt_id,
                    candidate_id=name,
                    artifact=artifact,
                    evaluation_id=EVALUATION["id"],
                    comparison_scope=EVALUATION["comparison_scope"],
                    evaluator=dict(id="synthetic-fixture", version="1"),
                    metrics={"score": score},
                ),
            )
            measurement = MeasurementRegistry(self.owner).register(iteration["id"], ref)
        return self.iteration(iteration["id"]), measurement

    def review(self, iteration, measurement=None, *, corrections=()):
        knowledge, decisions, refs = [], [], []
        if measurement is not None:
            refs.append(measurement["receipt_ref"])
            decisions.append(
                dict(
                    measurement_id=measurement["id"],
                    valid=True,
                    verified=True,
                    reason="Synthetic receipt matches declared fixture artifact",
                )
            )
            knowledge.append(
                dict(
                    id=f"observation-{measurement['candidate_id']}",
                    expected_revision=0,
                    kind="local_measurement",
                    statement=f"Synthetic receipt recorded for {measurement['candidate_id']}.",
                    conditions=["Deterministic local fixture only"],
                    source_refs=[],
                    measurement_ids=[measurement["id"]],
                    interpretation="",
                    limitations=["Not a scientific result or evaluator attestation"],
                )
            )
        for correction in corrections:
            refs.extend(ref for ref in correction["evidence_refs"] if ref not in refs)
        return self.accept(
            "review",
            dict(knowledge=knowledge, reviews=decisions, corrections=list(corrections)),
            iteration=iteration,
            refs=refs,
        )


def run_smoke(root=None):
    """Run in a temporary directory by default; return a detached result."""
    if root is None:
        with TemporaryDirectory(prefix="runtime-publication-") as temporary:
            return run_smoke(Path(temporary))
    store = make_store(root)
    with store.coordinator() as owner:
        demo, publisher = SyntheticSession(owner), Publisher(owner)
        first, a = demo.trial("candidate-a", 1.0)
        second, b = demo.trial("candidate-b", 2.0)
        review_a, review_b = demo.review(first, a), demo.review(second, b)
        publisher.publish(
            review_b
        )  # Creation order does not determine publication order.
        publisher.publish(review_a)
        writer = ProjectionWriter(owner)
        before = writer.rebuild()
        before_view = json.loads((store.root / before["frontiers_ref"]).read_text())[
            "global"
        ][EVALUATION["id"]]
        third, _ = demo.trial("qualitative-correction")
        correction = dict(
            measurement_id=b["id"],
            expected_revision=1,
            valid=False,
            verified=False,
            reason="Synthetic withdrawal to exercise eligibility history",
            evidence_refs=[b["receipt_ref"]],
        )
        publisher.publish(demo.review(third, corrections=[correction]))
        after = writer.rebuild()
        after_view = json.loads((store.root / after["frontiers_ref"]).read_text())[
            "global"
        ][EVALUATION["id"]]
        assert before_view["candidate_ids"] == [b["id"]]
        assert after_view["candidate_ids"] == [a["id"]]
        snapshot = publisher.snapshot()
        assert len(snapshot["measurements"]) == 2
        assert [row["metrics"]["score"] for row in snapshot["measurements"]] == [
            1.0,
            2.0,
        ]
        return dict(
            synthetic=True,
            version=after["version"],
            publications=len(snapshot["publications"]),
            measurements=2,
            iterations=store.snapshot()["counters"]["iterations"],
            frontier_before="candidate-b",
            frontier_after="candidate-a",
            restored=True,
        )


if __name__ == "__main__":
    result = run_smoke()
    print(
        f"Synthetic smoke: version={result['version']} publications={result['publications']} "
        f"measurements={result['measurements']} iterations={result['iterations']}; "
        f"frontier {result['frontier_before']} -> {result['frontier_after']}; restored={result['restored']}"
    )
