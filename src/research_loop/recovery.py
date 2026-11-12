"""Recovery-only evidence delivery and a completion-matched Lead driver.

Scientific text is authored and reviewed by independent model contexts. Python
copies existing evidence, binds review to the exact candidate and publishes it.
There is no experiment execution surface in this profile.
"""

from copy import deepcopy
import json
from pathlib import Path
import shutil

from .codex import Tool, configuration
from .group_agents import NativeGroupServices, _RunJournal
from .group_router import GroupCoordinator
from .group_programs.synthesis import (
    RECOVERY_INSTRUCTIONS,
    REVIEW_INSTRUCTIONS,
    _CRITIC,
    bind_recovery_analysis,
    recovery_author_schema,
    validate_analysis,
)
from .groups import GroupRuntime
from .state import now, write_json

_MAX_PACKET = 64000
_MAX_REPORT_BYTES = 64 * 1024
_REPORT_FIELDS = (
    "execution_ref",
    "evaluation_ref",
    "original_evaluation_ref",
    "recovery_evaluation_ref",
    "metrics_ref",
    "report_ref",
)


def _legacy_recovery_author_schema(schema, result_schema):
    """Recognize the two pinned recovery Author contracts, never infer from prose."""
    current = recovery_author_schema(result_schema)
    if schema == current:
        return False
    legacy = deepcopy(current)
    del legacy["properties"]["status"]
    legacy["required"].remove("status")
    if schema == legacy:
        return True
    raise ValueError("Unsupported recovery Author schema")


def evidence_packet(manifest_path):
    """Load explicit small reports, never input datasets, code or transcripts."""
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("warnings") or not manifest.get("evaluation_ref"):
        raise ValueError(
            "Recovery evidence has unresolved authority or delivery errors"
        )
    root = manifest_path.parent
    files, refs = [], []
    for field in _REPORT_FIELDS:
        ref = manifest.get(field)
        if ref is None and field == "recovery_evaluation_ref":
            continue
        if not isinstance(ref, str):
            raise ValueError(f"Missing recovery evidence {field}")
        path = Path(ref)
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
            raise ValueError(
                "Recovery report must belong to the frozen evidence bundle"
            )
        if not path.is_file() or path.stat().st_size > _MAX_REPORT_BYTES:
            raise ValueError("Recovery supports only small report files")
        content = path.read_text()
        files.append(
            {
                "kind": field.removesuffix("_ref"),
                "ref": ref,
                "content": json.loads(content) if path.suffix == ".json" else content,
            }
        )
        refs.append(ref)
    packet = {
        "kind": "exp-recovery-v1",
        **{field: manifest.get(field) for field in _REPORT_FIELDS},
        "source_run": manifest["source_run"],
        "limitations": manifest.get("limitations", []),
        "evidence_files": files,
        "publication_paths": {
            ref: "recovery-evidence/" + Path(ref).relative_to(root).as_posix()
            for ref in refs
        },
    }
    if len(json.dumps(packet, ensure_ascii=False)) > _MAX_PACKET:
        raise ValueError("Recovery evidence packet exceeds the model input budget")
    return packet


def stage_evidence(study_root, manifest_path):
    """Copy the explicit frozen deliverables into the unpublished knowledge draft."""
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text())
    evidence_packet(manifest_path)  # Authority/size check before any model call.
    sources = [manifest_path, Path(manifest["evaluation_ref"])]
    sources.extend(Path(item["ref"]) for item in manifest["artifacts"])
    draft = Path(study_root) / "knowledge/draft/recovery-evidence"
    for source in dict.fromkeys(sources):
        relative = source.relative_to(manifest_path.parent)
        if source.is_symlink() or not source.resolve(strict=True).is_relative_to(
            manifest_path.parent
        ):
            raise ValueError("Recovery deliverables must remain inside the bundle")
        target = draft / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class LeadRecoveryServices(NativeGroupServices):
    """Lead authors and requests a real independent review through one typed tool."""

    async def execute_prepared(self, *args, **kwargs):
        raise RuntimeError("Recovery cannot execute experiments")

    async def execute(self, *args, **kwargs):
        raise RuntimeError("Recovery cannot dispatch resource operations")

    async def call(self, role, context, instructions, payload, schema, **kwargs):
        if role == "critic":
            accepted = self.ctx.state.get("lead_accepted_candidate")
            if accepted is None or accepted != payload["analysis_record"]:
                raise ValueError(
                    "Lead final candidate lacks a matching independent review"
                )
            return deepcopy(self.ctx.state["lead_review"])
        if role != "analyst":
            raise ValueError(
                "Recovery Lead supports authorship and independent Critic only"
            )
        return await self._author(context, instructions, payload, schema)

    async def _author(self, context, instructions, sources, schema):
        self._admit()
        legacy = _legacy_recovery_author_schema(
            schema, self.study_context["output_schema"]
        )
        if not legacy:
            sources = {
                **sources,
                "review_protocol": {
                    **sources["review_protocol"],
                    "review_step": "through_review_candidate_tool",
                },
            }
        if self.ctx.state.get("lead_author_status") in {"running", "unknown"}:
            raise RuntimeError("Prior Lead call requires reconciliation")
        config, _ = configuration(
            self.runtime_root,
            Path(self.config["project"]),
            [self.ctx.work],
            self.config["model"],
            self.config["effort"],
            provider=self.config.get("provider"),
            delegation="none",
            allow_builtin_tools=False,
            execution_environment=self.config.get("execution_environment"),
        )

        async def review(candidate):
            canonical = bind_recovery_analysis(
                candidate, sources, legacy_recovery=legacy
            )
            ready = not canonical["help"] if legacy else canonical["status"] == "ready"
            if not ready:
                raise ValueError("Only a ready unreviewed candidate can request review")
            validate_analysis(
                canonical,
                self.study_context["output_schema"],
                sources,
                legacy_recovery=legacy,
            )
            if self.ctx.state.get("lead_accepted_candidate") == canonical:
                return {
                    "review": self.ctx.state["lead_review"],
                    "accepted_candidate": candidate,
                }
            reviews = self.ctx.state.setdefault("lead_reviews", [])
            if reviews:
                raise RuntimeError(
                    "Recovery review budget exhausted; no automatic review retry"
                )
            write_json(self.ctx.work / "lead-candidate.json", canonical)
            reviews.append({"status": "running", "at": now()})
            self.ctx.save_state()
            assessment = await super(LeadRecoveryServices, self).call(
                "critic",
                "lead-independent-review",
                REVIEW_INSTRUCTIONS + RECOVERY_INSTRUCTIONS,
                {
                    **sources,
                    "analysis_record": canonical,
                    "recovery_evidence": self.study_context["input"]["shared_input"],
                },
                _CRITIC,
                allow_tools=False,
                include_role_template=False,
                max_output_chars=4000,
            )
            reviews[-1].update(status="completed", verdict=assessment["verdict"])
            self.ctx.state["lead_review"] = assessment
            if assessment["verdict"] == "accept":
                self.ctx.state["lead_accepted_candidate"] = deepcopy(canonical)
            self.ctx.save_state()
            write_json(self.ctx.work / "lead-review.json", assessment)
            return {
                "review": assessment,
                "accepted_candidate": candidate
                if assessment["verdict"] == "accept"
                else None,
            }

        native = self.native_factory(
            config,
            _RunJournal(self.raw_journal, self.ctx.run_id, context),
            [
                Tool(
                    "review_candidate",
                    "Submit the complete publication candidate for one independent Critic review. Return the exact accepted candidate afterwards.",
                    schema,
                    review,
                    resource_effect=False,
                )
            ],
        )
        self.pending_calls += 1
        self.active.add(native)
        self.ctx.state["lead_author_status"] = "running"
        call = {"role": "lead", "context": context, "status": "running", "at": now()}
        self.ctx.state["native_calls"].append(call)
        self.ctx.save_state()

        def started(thread_id):
            self.ctx.state["lead_thread_id"] = thread_id
            self.ctx.save_state()
            self._roles(thread_id, context, "lead", "running")
            if getattr(self, "lead_started", None):
                self.lead_started(thread_id)

        try:
            await native.open()
            await native.verify(
                self.config["model"],
                self.config["effort"],
                provider=self.config.get("provider"),
            )
            model_sources = deepcopy(sources)
            model_sources.get("study_context", {}).get("input", {}).pop(
                "shared_input", None
            )
            result = await native.run(
                (
                    "You are the Lead for this recovery-only Study. Author its outcome and knowledge, "
                    "then call review_candidate with your complete candidate. That tool invokes an "
                    "independent Critic using the same frozen evidence. Obtain verdict=accept and "
                    "return exactly accepted_candidate, including every field. If upstream evidence "
                    "is missing, return help under the supplied legacy schema without requesting review. "
                    "No files, commands, experiments or other tools are available. "
                    "Never claim independent review without its receipt.\n"
                    if legacy
                    else "You are the Lead for this recovery-only Study. Author its outcome and knowledge, "
                    "then submit your ready UNREVIEWED candidate through review_candidate. "
                    "For this tool-enabled Lead, review_protocol selects that host review tool "
                    "instead of the leaf Author's return-then-review handoff. The tool invokes an "
                    "independent Critic using the same frozen evidence. For a ready candidate, "
                    "obtain verdict=accept and return exactly accepted_candidate, including every field. "
                    "For genuinely missing upstream evidence or authorization, return status=blocked "
                    "with meaningful help; do not request review or claim publication. No files, commands, "
                    "experiments or other tools are available. Never claim independent review without its receipt.\n"
                )
                + instructions,
                {
                    **model_sources,
                    "recovery_evidence": self.study_context["input"]["shared_input"],
                },
                schema,
                model=self.config["model"],
                effort=self.config["effort"],
                on_started=started,
                delegation="none",
                allow_builtin_tools=False,
                max_output_chars=12000,
            )
            result = bind_recovery_analysis(result, sources, legacy_recovery=legacy)
            call.update(status="completed", result=deepcopy(result))
            self.ctx.state["lead_author_status"] = "completed"
            ready = not result["help"] if legacy else result["status"] == "ready"
            if ready and result != self.ctx.state.get("lead_accepted_candidate"):
                raise ValueError(
                    "Lead must return the exact independently accepted candidate"
                )
            return result
        except BaseException:
            if call["status"] == "running":
                call["status"] = self.ctx.state["lead_author_status"] = "unknown"
            raise
        finally:
            try:
                await native.stop()
                await native.settle()
            finally:
                await native.close()
                self.active.discard(native)
                self.pending_calls -= 1
                thread = self.ctx.state.get("lead_thread_id")
                if thread:
                    self._roles(thread, context, "lead", call["status"])
                self.ctx.save_state()


class RecoveryLeadCoordinator(GroupCoordinator):
    """Controlled Lead comparison sharing the exact Group publication contract."""

    async def open(self):
        self.on_started = None

        def services(ctx):
            service = LeadRecoveryServices(
                ctx, self.config, self.journal, native_factory=self.native_factory
            )
            service.lead_started = lambda thread: (
                self.on_started(thread) if self.on_started else None
            )
            return service

        self.runtime = GroupRuntime(
            self.root,
            self.definitions,
            service_factory=services,
        )

    async def decide(self, packet, on_started):
        self.on_started = on_started
        if packet["runs"]:
            raise RuntimeError(
                "Recovery Lead could not complete its bounded publication task"
            )
        return {
            "action": "start",
            "group_id": self.config["study_result_group"],
            "run_id": "",
            "task_ref": packet["goal_ref"],
            "input_refs": packet["input_refs"],
            "prompt": "Recover and publish the existing settled experiment; never execute it.",
            "result_ref": "",
            "reason": "Python admits the recovery-only Lead task",
        }
