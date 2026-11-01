"""Read-only local dashboard. No model calls, remote commands or research mutations."""

import argparse
import json
import math
import sys
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from integrations.protok.backend import METRIC, PROTOCOL, ProTokBackend
from integrations.protok.run import job_path
from research_loop.harness import completed_response


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def prose(path):
    try:
        return path.read_text()[:250_000]
    except OSError:
        return "暂无记录"


def timestamp(value):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def duration(started, ended):
    start, end = timestamp(started), timestamp(ended)
    return max(0, end - start) if start is not None and end is not None else None


def brief(markdown):
    for line in (markdown or "").splitlines():
        line = re.sub(r"^[#>*\s-]+", "", line).strip()
        if line:
            return line[:240]
    return ""


def iteration_context(root, folder):
    meta = read(folder / "iteration.json", {})
    publication = read(folder / "publication.json", {})
    published = (
        meta.get("status") == "reviewed" and (folder / "publication.json").exists()
    )
    state = read(root / "study/state.json", {})
    current = state if str(state.get("iteration", "")).zfill(4) == folder.name else {}
    saved = publication.get("state_after", {}) if published else {}
    return meta, published, saved, current


def relative(path, root):
    return str(path.relative_to(root))


def agent_outputs(root, folder):
    """Small structured agent receipts only; partial responses are never outputs."""
    _, published, saved, current = iteration_context(root, folder)
    states = [saved, current]
    calls, accepted = {}, set()
    for state in states:
        for key, attempt in state.get("calls", {}).items():
            calls[attempt] = key
            if state.get("accepted_calls", {}).get(key):
                accepted.add(attempt)
    publication_refs = (
        {saved.get("plan_ref"), saved.get("review_ref")} if published else set()
    )
    accepted_refs = {
        state.get(name)
        for state in states
        for name in ("plan_ref", "review_ref")
        if state.get(name)
    }
    attempts = list((folder / "selection").glob("round-*/*/attempt-*"))
    for phase in ("plan", "review"):
        attempts.extend((folder / phase).glob("attempt-*"))
    entries = []
    for attempt in attempts:
        if not attempt.is_dir():
            continue
        parent = attempt.parent.name
        round_number, turn, side = None, None, None
        if attempt.parent.parent.name.startswith("round-"):
            round_number = int(attempt.parent.parent.name.split("-")[-1])
            if parent in ("a-proposal", "b-proposal"):
                side, phase = parent.split("-", 1)
                key = f"s{round_number - 1}-{side}"
            elif parent == "judge":
                phase = "judge"
                key = f"s{round_number - 1}-judge"
            else:
                match = re.fullmatch(r"(\d+)-([ab])-(critique|response)", parent)
                if not match:
                    continue
                turn, side, phase = match.groups()
                turn = int(turn)
                key = f"s{round_number - 1}-{turn - 1}-{side}-{phase}"
        else:
            phase = parent
            key = calls.get(relative(attempt, root / "study"), phase)
            if phase == "plan" and re.fullmatch(r"plan-\d+", key):
                round_number = int(key.split("-")[1]) + 1
        source = relative(attempt, root)
        study_attempt = relative(attempt, root / "study")
        reference = study_attempt + "/response.json"
        process = read(attempt / "process.json", {})
        output = completed_response(attempt)
        is_accepted = study_attempt in accepted or reference in accepted_refs
        is_published = reference in publication_refs
        status = (
            "completed" if output is not None else process.get("status", "unrecorded")
        )
        role = (
            f"Advocate {side.upper()}"
            if side
            else {"judge": "Judge", "plan": "Planner", "review": "Reviewer"}[phase]
        )
        entry = {
            "key": key,
            "phase": phase,
            "role": role,
            "round": round_number,
            "turn": turn,
            "status": status,
            "attempt": attempt.name,
            "started_at": process.get("started"),
            "ended_at": process.get("ended"),
            "duration_seconds": duration(process.get("started"), process.get("ended")),
            "source": source,
            "output": output,
            "error": process.get("error"),
            "accepted": is_accepted,
            "published": is_published,
        }
        # Receipt timestamps retain actual invocation order across retries/resampling.
        started = timestamp(process.get("started"))
        entry["_order"] = started if started is not None else attempt.stat().st_mtime
        entries.append(entry)
    entries.sort(key=lambda entry: (entry["_order"], entry["source"]))
    for entry in entries:
        del entry["_order"]
    return entries


def accepted_output(agents, phase):
    return next(
        (
            a["output"]
            for a in reversed(agents)
            if a["phase"] == phase and a["accepted"] and a["output"] is not None
        ),
        {},
    )


def activity(agents):
    return {
        "attempts": len(agents),
        "completed": sum(a["status"] == "completed" for a in agents),
        "accepted": sum(a["accepted"] for a in agents),
        "failed": sum(
            a["status"] in {"failed", "interrupted", "timed_out"} for a in agents
        ),
        "latest_role": agents[-1]["role"] if agents else None,
        "latest_status": agents[-1]["status"] if agents else None,
    }


def progress(root, state, backend, runner, agents):
    reasons = []

    def reason(title, detail, source):
        duplicate = next((item for item in reasons if item["detail"] == detail), None)
        if duplicate:
            if source not in duplicate["source"].split(" · "):
                duplicate["source"] += " · " + source
            return
        reasons.append({"title": title, "detail": detail, "source": source})

    if (root / "control/STOP").exists():
        reason("已设置暂停标记", prose(root / "control/STOP"), "control/STOP")
    if state.get("error"):
        reason("研究循环错误", state["error"], "study/state.json")
    control = read(root / "control/status.json", {})
    if control.get("error"):
        reason("控制器记录", control["error"], "control/status.json")
    if runner.get("error"):
        reason("运行器错误", runner["error"], "control/runner.json")
    elif runner.get("status") == "paused":
        reason(
            "运行器暂停记录", "运行器最近记录的状态为已暂停。", "control/runner.json"
        )
    if state.get("stop_reason"):
        reason("停止原因", state["stop_reason"], "study/state.json")
    if backend.get("outstanding"):
        reason(
            "评估记录未结束",
            f"{len(backend['outstanding'])} 条记录未完成或缺少终态、核账凭据；新增评估须等待完成或协调已有记录。",
            "backend.outstanding",
        )
    active = state.get("active_attempt")
    current_agent = next((a for a in agents if a["source"] == f"study/{active}"), None)
    process = {}
    if active:
        candidate = root / "study" / active
        # An Execute may wrap an agent subprocess. Read metadata, never its transcript.
        try:
            candidate.resolve().relative_to((root / "study").resolve())
        except ValueError:
            candidate = None
        if candidate is not None:
            process = read(candidate / "process.json", {}) or read(
                candidate / "agent/process.json", {}
            )
    started = (
        current_agent.get("started_at") if current_agent else process.get("started")
    )
    ended = current_agent.get("ended_at") if current_agent else process.get("ended")
    phase, status = state.get("phase"), state.get("status")
    phase_label = {
        "selection": "提案选择",
        "plan": "制定计划",
        "execute": "执行实验",
        "review": "独立评审",
        "done": "本轮结束",
    }.get(phase, "未知阶段")
    if status == "needs_attention":
        title, message = (
            "需要处理",
            f"循环停在 {phase_label}；请查看保留的错误与调用凭据。",
        )
    elif status == "stopped":
        title, message = "循环已停止", "停止状态与研究目标是否达成分别记录。"
    elif (root / "control/STOP").exists() or runner.get("status") == "paused":
        title, message = (
            "运行器已暂停",
            "控制记录禁止启动下一次评估；已有作业的终态以评估凭据为准。",
        )
    elif status == "running":
        title, message = (
            "调用进行中（记录状态）",
            f"当前阶段：{phase_label}；仅依据本地状态，未验证进程存活。",
        )
    elif phase == "done":
        title, message = (
            "本轮已结束",
            "尚未观察到下一轮调用；ready 状态本身不代表运行器正在运行。",
        )
    else:
        title, message = (
            "等待下一步",
            f"当前阶段：{phase_label}；尚未观察到正在运行的调用。",
        )
    if state.get("debugging") and not (root / "control/STOP").exists():
        title = "候选调试中"
        message = "正在处理已核账的候选实现失败；调试使用独立预算，通过后重新申请正式评价资源。"
        flow = read(
            root / "control/debug" / f"{state['iteration']:04d}" / "flow.json", {}
        )
        if flow.get("phase") in {"formal_prepare", "formal"}:
            title = "正式评价中（调试已通过）"
            message = "正在为修复后的候选运行新的完整正式评价作业；调试消耗单独记账。"
        failure = read(root / "study" / state["debugging"], {})
        cause = failure.get("details", {}).get("cause")
        if cause:
            reason("本次调试原因", cause, "study/" + state["debugging"])
    stages = []
    order = ["selection", "plan", "execute", "review"]
    for key in order:
        relevant = [
            a
            for a in agents
            if (
                "selection"
                if a["phase"] in {"proposal", "critique", "response", "judge"}
                else a["phase"]
            )
            == key
        ]
        stage_status = "pending"
        if relevant:
            stage_status = relevant[-1]["status"]
        if key == "execute":
            count = (
                len(
                    list(
                        (
                            root
                            / "study/iterations"
                            / f"{state.get('iteration', 0):04d}"
                            / "execute"
                        ).glob("attempt-*")
                    )
                )
                if isinstance(state.get("iteration", 0), int)
                else 0
            )
            if state.get("accepted_calls", {}).get("execute"):
                stage_status = "completed"
            stage_duration = (
                duration(process.get("started"), process.get("ended"))
                if phase == "execute"
                else None
            )
        else:
            count = len(relevant)
            timings = [
                a["duration_seconds"]
                for a in relevant
                if a["duration_seconds"] is not None
            ]
            stage_duration = sum(timings) if timings else None
        if phase == key:
            stage_status = status or "unknown"
        stages.append(
            {
                "key": key,
                "status": stage_status,
                "attempts": count,
                "duration_seconds": stage_duration,
            }
        )
    return {
        "title": title,
        "message": message,
        "reasons": reasons,
        "active_role": current_agent["role"]
        if current_agent
        else ("Executor" if phase == "execute" and active else None),
        "attempt": active,
        "started_at": started,
        "elapsed_seconds": duration(
            started, ended if ended is not None else time.time()
        ),
        "stages": stages,
    }


def scout_snapshot(root):
    if not read(root / "study/study.json", {}).get("exploration"):
        return None
    status = read(root / "control/scout/status.json", {})
    envelope = read(root / "control/scout/latest.json", {})
    result = envelope.get("result", {})
    return {
        "status": status.get("status", "unrecorded"),
        "summary": result.get("summary", ""),
        "error": status.get("error"),
        "findings": result.get("candidates", []),
        "sources": result.get("sources", []),
        "search_status": result.get("search_status"),
        "search_notes": result.get("search_notes"),
        "evidence_status": envelope.get("evidence_status"),
        "source_claims_verified": envelope.get("source_claims_verified", False),
        "started_at": status.get("started_at"),
        "finished_at": status.get("finished_at"),
        "findings_finished_at": envelope.get("finished_at"),
        "published_through": envelope.get("published_through"),
        "source": "control/scout/latest.json"
        if envelope
        else "control/scout/status.json",
    }


def frontier_snapshot(root):
    """Display canonical, published RMSD metadata without grading experiments again."""
    definition = read(root / "study/study.json", {}).get("global_evaluation") or {}
    evaluation_id, scope = definition.get("id"), definition.get("comparison_scope")
    result = {
        "available": False,
        "evaluation_id": evaluation_id,
        "comparison_scope": scope,
        "metric": METRIC,
        "unit": "Å",
        "direction": "minimize",
        "current_candidate_ids": [],
        "current_experiment_ids": [],
        "canonical_candidate_ids": [],
        "current_membership_complete": False,
        "points": [],
        "excluded": [],
        "source": "study/frontiers.json",
        "history_basis": "accepted_publication_frontier",
        "improvement_basis": "exact_decrease_from_prepublication_frontier",
    }
    if (
        evaluation_id != PROTOCOL
        or not scope
        or definition.get("allow_feedback") is not True
        or len(definition.get("objectives", [])) != 1
        or definition["objectives"][0].get("name") != METRIC
        or definition["objectives"][0].get("direction") != "minimize"
    ):
        result["reason"] = "固定 RMSD 最小化全局评价定义不可用。"
        return result

    def matching(view):
        return (
            isinstance(view, dict)
            and view.get("evaluation_id") == evaluation_id
            and view.get("comparison_scope") == scope
            and isinstance(view.get("candidate_ids"), list)
        )

    canonical = (
        read(root / "study/frontiers.json", {}).get("global", {}).get(evaluation_id)
    )
    if not matching(canonical) or canonical.get("allow_feedback") is False:
        result["reason"] = (
            "缺少同一评价和比较范围的当前 canonical Frontier；不推断当前成员。"
        )
        return result
    current_ids = set(canonical["candidate_ids"])
    exclusions = {
        entry["id"]: entry.get("reason") for entry in canonical.get("excluded", [])
    }
    records, known = [], {}
    for folder in sorted((root / "study/iterations").glob("[0-9]*")):
        metadata = read(folder / "iteration.json", {})
        if (
            metadata.get("status") != "reviewed"
            or not (folder / "publication.json").exists()
        ):
            continue
        record = read(folder / "candidates.json", {})
        # Corrections have exactly the same eligibility-only meaning as _build_views.
        for correction in record.get("corrections", []):
            candidate = known.get(correction.get("candidate_id"))
            if candidate is not None:
                candidate.update(
                    valid=correction.get("valid"), verified=correction.get("verified")
                )
        candidates = [dict(candidate) for candidate in record.get("candidates", [])]
        for candidate in candidates:
            known[candidate.get("id")] = candidate
        records.append((folder, metadata, record, candidates))

    def embedded_view(publication, section):
        contents = publication.get(section, {}).get("frontiers.json")
        try:
            contents = json.loads(contents) if isinstance(contents, str) else contents
        except ValueError:
            return None
        return contents if isinstance(contents, dict) else None

    def metric_value(candidate):
        value = candidate.get("metrics", {}).get(METRIC)
        return value if type(value) in (int, float) and math.isfinite(value) else None

    for folder, metadata, record, candidates in records:
        if record.get("global_evaluation_id") != evaluation_id:
            continue
        publication = read(folder / "publication.json", {})
        after = embedded_view(publication, "writes")
        historical = after.get("global", {}).get(evaluation_id) if after else None
        before = embedded_view(publication, "before")
        prior = before.get("global", {}).get(evaluation_id) if before else None
        prior_known = matching(prior) or (
            isinstance(before, dict)
            and isinstance(before.get("global"), dict)
            and evaluation_id not in before["global"]
        )
        previous_values = [metric_value(c) for c in (prior or {}).get("candidates", [])]
        previous_values = [value for value in previous_values if value is not None]
        prior_ids = (prior or {}).get("candidate_ids", [])
        previous_best = min(previous_values) if previous_values else None
        if prior_ids and (
            not previous_values or len(previous_values) != len(prior_ids)
        ):
            prior_known = False
        imported = (
            read(job_path(root, folder.name) / "result.json", {}).get("provenance")
            == "historical-import-no-allocation"
        )
        for candidate in candidates:
            identity = candidate.get("id")
            value = metric_value(candidate)
            if (
                candidate.get("evaluation_id") != evaluation_id
                or candidate.get("comparison_scope") != scope
                or candidate.get("verified") is not True
                or candidate.get("valid") is not True
                or candidate.get("experiment_id") != metadata.get("id")
                or value is None
                or (
                    identity not in current_ids
                    and exclusions.get(identity) != "dominated"
                )
            ):
                continue
            reached = (
                identity in historical["candidate_ids"]
                if matching(historical)
                else None
            )
            improved = (
                (value < previous_best if previous_best is not None else False)
                if reached is True and prior_known
                else False
                if reached is False
                else None
            )
            result["points"].append(
                {
                    "id": candidate["experiment_id"],
                    "candidate_id": identity,
                    "value": value,
                    "current": identity in current_ids,
                    "reached": reached,
                    "improved": improved,
                    "previous_best": previous_best if prior_known else None,
                    "delta": previous_best - value
                    if improved
                    else 0
                    if prior_known and previous_best is not None
                    else None,
                    "imported": imported,
                    "source": relative(folder / "candidates.json", root),
                    "historical_frontier_source": relative(
                        folder / "publication.json", root
                    )
                    + "#writes.frontiers.json"
                    if matching(historical)
                    else None,
                    "previous_frontier_source": relative(
                        folder / "publication.json", root
                    )
                    + "#before.frontiers.json"
                    if prior_known
                    else None,
                }
            )
    published_current = {
        point["candidate_id"] for point in result["points"] if point["current"]
    }
    result.update(
        available=True,
        canonical_candidate_ids=list(canonical["candidate_ids"]),
        current_membership_complete=published_current == current_ids,
        current_candidate_ids=[
            identity
            for identity in canonical["candidate_ids"]
            if identity in published_current
        ],
        current_experiment_ids=sorted(
            {point["id"] for point in result["points"] if point["current"]}
        ),
        excluded=[
            {
                "id": entry["id"].split(":", 1)[0],
                "candidate_id": entry["id"],
                "reason": entry.get("reason"),
            }
            for entry in canonical.get("excluded", [])
        ],
    )
    return result


def snapshot(root, *, snapshot_only=False):
    study = root / "study"
    state = read(study / "state.json", {})
    best = read(root / "control/active-best.json", {})
    if snapshot_only:
        backend = read(root / "control/backend-snapshot.json")
        if not backend:
            raise ValueError("Missing exported backend snapshot")
    else:
        owner = read(root / "control/ownership.json", {})
        backend = ProTokBackend(Path(owner["project"]), root / "control").inspect()
    records = {r["fingerprint"]: r for r in backend["records"]}
    nodes = []
    for folder in sorted((study / "iterations").glob("[0-9]*")):
        meta = read(folder / "iteration.json", {})
        if not meta:
            continue
        identity = meta["id"]
        job = read(job_path(root, identity) / "job.json", {})
        receipt = read(job_path(root, identity) / "result.json", {})
        record = records.get(job.get("fingerprint"), {})
        agents = agent_outputs(root, folder)
        _, published, saved, current = iteration_context(root, folder)
        review = (
            next(
                (
                    a["output"]
                    for a in agents
                    if a["phase"] == "review"
                    and a["published"]
                    and a["output"] is not None
                ),
                {},
            )
            if published
            else accepted_output(agents, "review")
        )
        plan_ref = current.get("plan_ref") or saved.get("plan_ref")
        accepted_plan = next(
            (
                a["output"]
                for a in agents
                if a["phase"] == "plan"
                and a["source"] + "/response.json" == "study/" + str(plan_ref)
                and a["output"] is not None
            ),
            {},
        )
        selected = current.get("selected") or saved.get("selected") or {}
        judge = selected.get("judge", {})
        proposal = selected.get("proposal", {})
        proposal_action = proposal.get("action")
        # Some retained proposals record an action category, with intent in prose.
        if proposal_action in {"propose", "evaluate", "resume", "import"}:
            proposal_action = proposal.get("markdown")
        eligible = published and any(
            c.get("valid")
            and c.get("verified")
            and c.get("candidate_id") == receipt.get("fingerprint")
            for c in review.get("candidate_reviews", [])
        )
        active = int(identity) == state.get("iteration")
        live = state.get("status") if active else "reviewed"
        plan = prose(folder / "plan.md")
        nodes.append(
            {
                "id": identity,
                "route": meta.get("route_title", "路线选择中"),
                "route_id": meta.get("route_id"),
                "base": meta.get("base_experiment"),
                "references": meta.get("references", []),
                "status": live
                if active
                else ("reviewed" if published else meta.get("status")),
                "phase": state.get("phase") if active else "done",
                "published": published,
                "eligible": eligible,
                "value": receipt.get("value") if eligible else None,
                "provisional_value": record.get("summary", {}).get("value")
                if not eligible
                else None,
                "imported": receipt.get("provenance")
                == "historical-import-no-allocation",
                "attempts": record.get("attempts", []),
                "job_status": job.get("status"),
                "source_commit": job.get("source_commit"),
                "title": plan.split("\n")[0][:150],
                "action": accepted_plan.get("contract", {}).get("action"),
                "action_summary": brief(proposal_action)
                or brief(judge.get("action"))
                or brief(accepted_plan.get("markdown"))
                or (brief(plan) if (folder / "plan.md").exists() else ""),
                "action_summary_label": "研究意图（所选提案）"
                if proposal_action
                else "选定行动（Judge）"
                if judge.get("action")
                else "计划摘要",
                "inquiry_kind": review.get("insight", {}).get("actual_kind")
                or proposal.get("research_design", {}).get("kind")
                or read(folder / "selection.json", {})
                .get("rounds", {})
                .get(
                    str(
                        current.get("selection_round", saved.get("selection_round", 0))
                    ),
                    {},
                )
                .get("exploration", {})
                .get("kind"),
                "summary": brief(review.get("markdown"))
                or (
                    brief(prose(folder / "review.md"))
                    if published and (folder / "review.md").exists()
                    else ""
                ),
                "goal_status": review.get("goal_status"),
                "review_decision": review.get("decision"),
                "agent_activity": activity(agents),
                "created_at": meta.get("created_at"),
            }
        )
    frontier = frontier_snapshot(root)
    for node in nodes:
        points = [point for point in frontier["points"] if point["id"] == node["id"]]
        node["fingerprint"] = read(job_path(root, node["id"]) / "job.json", {}).get(
            "fingerprint"
        ) or read(job_path(root, node["id"]) / "result.json", {}).get("fingerprint")
        if points:
            candidate = next(
                (
                    c
                    for c in read(
                        study / "iterations" / node["id"] / "candidates.json", {}
                    ).get("candidates", [])
                    if c.get("id") == points[0]["candidate_id"]
                ),
                {},
            )
            node["fingerprint"] = node["fingerprint"] or candidate.get(
                "artifact", {}
            ).get("version")
            node["value"] = points[0]["value"]
            node["eligible"] = True
        node["evaluation_id"] = frontier["evaluation_id"] if points else None
        node["comparison_scope"] = frontier["comparison_scope"] if points else None
        node["frontier_current"] = (
            node["id"] in frontier["current_experiment_ids"]
            if frontier["available"]
            else None
        )
        node["frontier_exclusion_reason"] = (
            "; ".join(
                sorted(
                    {
                        entry["reason"]
                        for entry in frontier["excluded"]
                        if entry["id"] == node["id"]
                        and entry.get("reason")
                        and entry["reason"] != "dominated"
                    }
                )
            )
            or None
        )
        if node["frontier_exclusion_reason"]:
            node["eligible"] = False
            node["value"] = None
        reached = [point["reached"] for point in points]
        node["frontier_reached"] = (
            True
            if any(value is True for value in reached)
            else False
            if reached and all(value is False for value in reached)
            else None
        )
    runner = read(root / "control/runner.json", {})
    current_folder = study / "iterations" / f"{state.get('iteration', 0):04d}"
    current_agents = (
        agent_outputs(root, current_folder) if current_folder.exists() else []
    )
    continuation = read(root / "control/continuation.json", {})
    ownership = read(root / "control/ownership.json", {})
    deadlines = [
        value
        for value in (
            timestamp(continuation.get("deadline")),
            timestamp(ownership.get("expires_at")),
        )
        if value is not None
    ]
    debug_root = root / "control/debug" / f"{state.get('iteration', 0):04d}"
    debug_budget = read(debug_root / "debug-budget.json", {})
    debug_flow = read(debug_root / "flow.json", {})
    active_debug = debug_budget.get("attempts", {}).get(
        debug_budget.get("active_attempt"), {}
    )
    used_debug = debug_budget.get("used_gpu_seconds", 0)
    reserved_debug = active_debug.get("allocated_gpu_seconds", 0)
    debug_limit = debug_budget.get("limit_gpu_seconds", 1800)
    return {
        "observed_at": time.time(),
        "study_name": root.name,
        "study_root": str(root),
        "snapshot_only": snapshot_only,
        "backend_observed_at": backend.get("observed_at"),
        "state": {
            k: state.get(k)
            for k in (
                "iteration",
                "phase",
                "status",
                "call_count",
                "error",
                "active_attempt",
                "stop_reason",
                "goal_status",
                "debugging",
            )
        },
        "deadline": continuation.get("deadline"),
        "effective_deadline": min(deadlines) if deadlines else None,
        "stop": (root / "control/STOP").exists(),
        "runner": runner,
        "progress": progress(root, state, backend, runner, current_agents),
        "scout": scout_snapshot(root),
        "best": {"id": best.get("experiment_id"), "metrics": best.get("metrics", {})},
        "gpu_seconds": backend.get("total_gpu_seconds", backend["used_gpu_seconds"]),
        "formal_gpu_seconds": backend["used_gpu_seconds"],
        "debug_gpu_seconds": backend.get("debug_gpu_seconds", 0),
        "debug": {
            "phase": debug_flow.get("phase"),
            "used_gpu_seconds": used_debug,
            "reserved_gpu_seconds": reserved_debug,
            "limit_gpu_seconds": debug_limit,
            "remaining_gpu_seconds": max(0, debug_limit - used_debug - reserved_debug),
        }
        if debug_budget or state.get("debugging")
        else None,
        "scored_records": backend["scored_records"],
        "outstanding": backend["outstanding"],
        "nodes": nodes,
        "frontier": frontier,
        "routes": read(study / "routes/index.json", {}),
    }


def details(root, identity):
    folder = root / "study/iterations" / identity
    selection = read(folder / "selection.json", {})
    return {
        "plan": prose(folder / "plan.md"),
        "review": prose(folder / "review.md"),
        "selection": selection,
        "agents": agent_outputs(root, folder),
        "metadata": read(folder / "iteration.json", {}),
        "sources": {
            key: relative(folder / name, root)
            for key, name in (
                ("plan", "plan.md"),
                ("review", "review.md"),
                ("route", "route.after.md"),
                ("selection", "selection.json"),
            )
        },
        "route": prose(folder / "route.after.md"),
    }


def study_sources(root, history_roots=()):
    return [
        {"id": f"history-{index}", "label": f"历史{index}", "root": Path(path)}
        for index, path in enumerate(history_roots, 1)
    ] + [{"id": "active", "label": "当前", "root": Path(root)}]


def history_cutoff(root, history_roots=()):
    roots = [root, *history_roots]
    times = [
        timestamp(read(path, {}).get("created_at"))
        for source in roots
        for path in (Path(source) / "study/iterations").glob("[0-9]*/iteration.json")
    ]
    observed = [value for value in times if value is not None]
    return min(observed) if observed else None, len(times) - len(observed)


def ledger_history(records_root, cutoff):
    """Only explicit small evaluator metadata; no assumed scope or Review."""
    nodes, points, limitations = [], [], []
    missing_time = later = 0
    if records_root is None:
        return (
            nodes,
            points,
            limitations,
            {"missing_timestamps": 0, "at_or_after_cutoff": 0},
        )
    if cutoff is None:
        limitations.append(
            "历史 Study 缺少可用创建时间，无法确定前置账本范围；未排列账本记录。"
        )
    for path in sorted(Path(records_root).glob("*/state.json")):
        state = read(path, {})
        created = state.get("created_at")
        when = timestamp(created)
        if when is None:
            missing_time += 1
            continue
        if cutoff is None or when >= cutoff:
            later += 1
            continue
        fingerprint = state.get("fingerprint")
        record_name = path.parent.name
        record_mode = state.get("mode")
        diagnostic = record_mode == "smoke"
        if (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or not (
                record_name == fingerprint
                or (diagnostic and record_name == "smoke-" + fingerprint)
            )
        ):
            limitations.append(f"{path.parent.name} 的记录身份不完整，未纳入账本视图。")
            continue
        summary = read(path.parent / "summary.json", {}) or state.get("summary", {})
        config = read(path.parent / "run-config.json", {})
        protocol = summary.get("protocol") or config.get("protocol")
        metric = summary.get("objective") or config.get("metric")
        value = summary.get("value")
        same_protocol = protocol == PROTOCOL and metric == METRIC
        finite = type(value) in (int, float) and math.isfinite(value)
        usable = (
            same_protocol
            and finite
            and record_mode == "scored"
            and summary.get("quality_score_eligible") is True
            and summary.get("fingerprint", fingerprint) == fingerprint
        )
        identity = "ledger:" + record_name
        node = {
            "id": identity,
            "local_id": record_name,
            "source_id": "ledger",
            "source_label": "前置账本",
            "label": ("历史检查 " if diagnostic else "历史评价 ") + fingerprint[:10],
            "mode": record_mode,
            "record_mode": record_mode,
            "diagnostic": diagnostic,
            "route": "历史冒烟检查" if diagnostic else "前置固定协议账本",
            "route_id": None,
            "base": None,
            "declared_base": None,
            "references": [],
            "status": state.get("status", "unrecorded"),
            "phase": "ledger",
            "published": False,
            "eligible": False,
            "reviewed": False,
            "frontier_eligible": False,
            "value": value if usable else None,
            "provisional_value": value if usable else None,
            "observed_value": value if same_protocol and finite else None,
            "fingerprint": fingerprint,
            "evaluation_id": protocol,
            "comparison_scope": None,
            "imported": False,
            "attempts": [
                {
                    key: attempt[key]
                    for key in (
                        "seed",
                        "status",
                        "owned_job_id",
                        "terminal_state",
                        "exit_code",
                        "gpu_seconds",
                    )
                    if key in attempt
                }
                for attempt in state.get("attempts", [])
            ],
            "job_status": state.get("status"),
            "title": "历史冒烟检查（诊断，不计正式 RMSD）"
            if diagnostic
            else "未经独立 Review 的历史评价",
            "action_summary": (
                "冒烟检查：" + brief(state.get("error"))
                if state.get("error")
                else "历史冒烟检查；诊断记录，不计正式 RMSD。"
            )
            if diagnostic
            else brief(state.get("error"))
            or "原始固定协议历史评价；仅展示已记录元数据。",
            "action_summary_label": "账本记录",
            "action": None,
            "summary": "诊断检查，不计正式 RMSD 或 Frontier。"
            if diagnostic
            else "未经 Study 独立 Review，不进入 reviewed Frontier。",
            "created_at": created,
            "error": state.get("error"),
            "detail_url": "/api/node/ledger/" + record_name,
            "frontier_current": False,
            "frontier_reached": None,
            "frontier_exclusion_reason": "diagnostic_smoke"
            if diagnostic
            else "unreviewed_ledger",
            "quality_score_eligible": summary.get("quality_score_eligible"),
        }
        nodes.append(node)
        if usable:
            points.append(
                {
                    "id": identity,
                    "candidate_id": identity,
                    "original_candidate_id": fingerprint,
                    "source_id": "ledger",
                    "source_label": "前置账本",
                    "source": "ledger",
                    "value": value,
                    "fingerprint": fingerprint,
                    "evaluation_id": protocol,
                    "comparison_scope": None,
                    "reviewed": False,
                    "frontier_eligible": False,
                    "current": False,
                    "reached": None,
                    "improved": None,
                    "previous_best": None,
                    "delta": None,
                    "imported": False,
                }
            )
    if missing_time:
        limitations.append(
            f"{missing_time} 条账本记录缺少创建时间，未推断先后或纳入前置历史。"
        )
    return (
        nodes,
        points,
        limitations,
        {"missing_timestamps": missing_time, "at_or_after_cutoff": later},
    )


def view_snapshot(root, *, snapshot_only=False, history_roots=(), records_root=None):
    """Join explicitly configured histories; active state/accounting stays authoritative."""
    root = Path(root)
    definitions = study_sources(root, history_roots)
    active = snapshot(root, snapshot_only=snapshot_only)
    nodes, sources, points, routes, limitations = [], [], [], {}, []
    cutoff, missing_cutoff_times = history_cutoff(root, history_roots)
    ledger_nodes, legacy_points, notes, ledger_coverage = ledger_history(
        records_root, cutoff
    )
    limitations.extend(notes)
    if missing_cutoff_times:
        limitations.append(
            f"{missing_cutoff_times} 个 Study 迭代缺少创建时间，账本截止点仅依据已记录时间。"
        )
    if records_root is not None:
        sources.append(
            {
                "id": "ledger",
                "label": "前置账本",
                "name": Path(records_root).name,
                "root": str(records_root),
                "mode": "unreviewed_ledger",
                "count": len(ledger_nodes),
            }
        )
        nodes.extend(ledger_nodes)
    active_frontier = active["frontier"]
    history_frontiers = []
    for definition in definitions:
        source_id, source_root, label = (
            definition["id"],
            definition["root"],
            definition["label"],
        )
        local = (
            active
            if source_id == "active"
            else snapshot(source_root, snapshot_only=True)
        )
        source_frontier = local["frontier"]
        compatible = all(
            source_frontier.get(key) == active_frontier.get(key)
            for key in ("evaluation_id", "comparison_scope", "metric", "direction")
        )
        sources.append(
            {
                "id": source_id,
                "label": label,
                "name": source_root.name,
                "root": str(source_root),
                "mode": "snapshot"
                if source_id != "active" or snapshot_only
                else "local_records",
                "count": len(local["nodes"]),
            }
        )
        history_frontiers.append(
            {
                "source_id": source_id,
                "available": source_frontier["available"],
                "compatible": compatible,
                "reason": source_frontier.get("reason"),
                "current_membership_complete": source_frontier[
                    "current_membership_complete"
                ],
            }
        )
        if not source_frontier["available"]:
            limitations.append(
                f"{label}缺少可用 canonical Frontier：{source_frontier.get('reason', '未记录')}。"
            )
        elif not compatible:
            limitations.append(
                f"{label}的评价或比较范围与当前 Study 不同；未合并其 Frontier 曲线。"
            )
        for node in local["nodes"]:
            original = node["id"]
            merged = {
                **node,
                "id": source_id + ":" + original,
                "local_id": original,
                "source_id": source_id,
                "source_label": label,
                "label": label + " #" + original,
                "declared_base": node.get("base"),
                "base": source_id + ":" + node["base"] if node.get("base") else None,
                "references": [
                    source_id + ":" + ref for ref in node.get("references", [])
                ],
                "route_id": source_id + ":" + node["route_id"]
                if node.get("route_id")
                else None,
                "detail_url": "/api/node/" + source_id + "/" + original,
                "reviewed": node.get("published", False),
                "frontier_eligible": compatible
                and any(p["id"] == original for p in source_frontier["points"]),
                "frontier_local_current": node.get("frontier_current"),
                "frontier_current": node.get("frontier_current")
                if source_id == "active"
                else False,
            }
            nodes.append(merged)
        for route_id, route in local.get("routes", {}).items():
            routes[source_id + ":" + route_id] = {
                **route,
                "source_id": source_id,
                "source_label": label,
                "local_id": route_id,
            }
        if source_frontier["available"] and compatible:
            for point in source_frontier["points"]:
                points.append(
                    {
                        **point,
                        "id": source_id + ":" + point["id"],
                        "candidate_id": source_id + ":" + point["candidate_id"],
                        "original_candidate_id": point["candidate_id"],
                        "source_id": source_id,
                        "source_label": label,
                        "source": source_id,
                        "evidence_source": point["source"],
                        "local_current": point["current"],
                        "current": point["current"] if source_id == "active" else False,
                        "evaluation_id": source_frontier["evaluation_id"],
                        "comparison_scope": source_frontier["comparison_scope"],
                        "reviewed": True,
                        "frontier_eligible": True,
                    }
                )
    # Undated nodes remain visibly undated and use only configured source/local order.
    undated = [node for node in nodes if timestamp(node.get("created_at")) is None]
    if undated:
        limitations.append(
            f"{len(undated)} 个迭代缺少创建时间；置于已定时记录之后，按配置来源与本地编号排列，不宣称其跨来源先后。"
        )
    nodes.sort(
        key=lambda node: (
            timestamp(node.get("created_at")) is None,
            timestamp(node.get("created_at")) or 0,
        )
    )
    by_id = {}
    for sequence, node in enumerate(nodes, 1):
        node["sequence"] = sequence
        node["ordering"] = (
            "observed_timestamp"
            if timestamp(node.get("created_at")) is not None
            else "undated_source_order"
        )
        by_id[node["id"]] = node
    scoped_results, ledger_results = {}, {}
    for node in nodes:
        fingerprint = node.get("fingerprint")
        if not fingerprint:
            continue
        protocol, scope = node.get("evaluation_id"), node.get("comparison_scope")
        if node["source_id"] == "ledger":
            if (
                node.get("record_mode") == "scored"
                and node.get("quality_score_eligible") is True
                and node.get("value") is not None
            ):
                ledger_results[(fingerprint, protocol)] = node["id"]
        elif node.get("frontier_eligible") and scope:
            key = (fingerprint, protocol, scope)
            previous = scoped_results.get(key)
            if previous:
                node["duplicate_of"] = previous
                if node.get("imported"):
                    node["imported_from"] = previous
                if by_id[previous].get("value") != node.get("value"):
                    node["duplicate_value_conflict"] = True
                    limitations.append(
                        f"{node['label']}与同指纹记录数值不同；保留冲突，未视为新的独立结果。"
                    )
            else:
                scoped_results[key] = node["id"]
            if (fingerprint, protocol) in ledger_results:
                node["same_ledger_result"] = ledger_results[(fingerprint, protocol)]
    for point in points + legacy_points:
        node = by_id[point["id"]]
        point["sequence"] = node["sequence"]
        point["fingerprint"] = node.get("fingerprint")
        point["duplicate_of"] = node.get("duplicate_of")
        point["same_ledger_result"] = node.get("same_ledger_result")
    points.sort(key=lambda point: point["sequence"])
    legacy_points.sort(key=lambda point: point["sequence"])
    merged_frontier = {
        **active_frontier,
        "current_candidate_ids": [
            "active:" + value for value in active_frontier["current_candidate_ids"]
        ],
        "canonical_candidate_ids": [
            "active:" + value for value in active_frontier["canonical_candidate_ids"]
        ],
        "current_experiment_ids": [
            "active:" + value for value in active_frontier["current_experiment_ids"]
        ],
        "points": points,
        "legacy_points": legacy_points,
        "sources": history_frontiers,
        "source": "active",
        "evidence_source": active_frontier["source"],
        "current_source_id": "active",
        "excluded": [
            {
                **entry,
                "id": "active:" + entry["id"],
                "candidate_id": "active:" + entry["candidate_id"],
                "original_candidate_id": entry["candidate_id"],
            }
            for entry in active_frontier["excluded"]
        ],
    }
    observed_result_keys = {
        (node.get("fingerprint"), node.get("evaluation_id"))
        for node in nodes
        if node.get("fingerprint") and node.get("value") is not None
    }
    return {
        **active,
        "nodes": nodes,
        "routes": routes,
        "sources": sources,
        "active_node_id": "active:" + f"{active['state'].get('iteration') or 0:04d}"
        if active["nodes"]
        else None,
        "frontier": merged_frontier,
        "coverage": {
            "study_count": len(definitions),
            "node_count": len(nodes),
            "reviewed_point_count": len(points),
            "unique_reviewed_result_count": len(scoped_results),
            "duplicate_reviewed_result_count": sum(
                bool(point.get("duplicate_of")) for point in points
            ),
            "ledger_record_count": len(ledger_nodes),
            "ledger_smoke_count": sum(
                node.get("record_mode") == "smoke" for node in ledger_nodes
            ),
            "ledger_point_count": len(legacy_points),
            "unique_observed_fingerprint_count": len(observed_result_keys),
            "ledger_cutoff": cutoff,
            "ledger": ledger_coverage,
            "limitations": limitations,
        },
    }


def source_details(root, source_id, local_id, *, history_roots=(), records_root=None):
    definitions = {
        source["id"]: source for source in study_sources(root, history_roots)
    }
    if source_id == "ledger":
        if records_root is None or not re.fullmatch(
            r"(?:smoke-)?[0-9a-f]{64}", local_id
        ):
            raise ValueError("Unknown ledger source or record")
        cutoff, _ = history_cutoff(root, history_roots)
        nodes, _, _, _ = ledger_history(records_root, cutoff)
        node = next((node for node in nodes if node["local_id"] == local_id), None)
        if node is None:
            raise ValueError("Ledger record is outside configured history")
        return {
            "source_id": "ledger",
            "local_id": local_id,
            "label": node["label"],
            "reviewed": False,
            "frontier_eligible": False,
            "summary": node["summary"],
            "record_mode": node["record_mode"],
            "diagnostic": node["diagnostic"],
            "ledger": {
                key: node.get(key)
                for key in (
                    "fingerprint",
                    "mode",
                    "record_mode",
                    "diagnostic",
                    "observed_value",
                    "evaluation_id",
                    "comparison_scope",
                    "value",
                    "status",
                    "created_at",
                    "error",
                    "attempts",
                    "quality_score_eligible",
                )
            },
            "sources": {
                key: str(Path(records_root) / local_id / (key + ".json"))
                for key in ("state", "summary", "run-config")
            },
        }
    if source_id not in definitions or not re.fullmatch(r"[0-9]{4,}", local_id):
        raise ValueError("Unknown configured Study source or iteration")
    definition = definitions[source_id]
    if not (
        definition["root"] / "study/iterations" / local_id / "iteration.json"
    ).is_file():
        raise ValueError("Unknown configured iteration")
    return {
        **details(definition["root"], local_id),
        "source_id": source_id,
        "local_id": local_id,
        "source_label": definition["label"],
        "study_root": str(definition["root"]),
    }


def serve(root, port, *, snapshot_only=False, history_roots=(), records_root=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    body = Path(__file__).with_suffix(".html").read_bytes()
                    mime = "text/html; charset=utf-8"
                elif path == "/api/snapshot":
                    body = json.dumps(
                        view_snapshot(
                            root,
                            snapshot_only=snapshot_only,
                            history_roots=history_roots,
                            records_root=records_root,
                        ),
                        ensure_ascii=False,
                    ).encode()
                    mime = "application/json; charset=utf-8"
                elif re.fullmatch(
                    r"/api/node/(?:active|history-[1-9][0-9]*)/[0-9]{4,}|/api/node/ledger/(?:smoke-)?[0-9a-f]{64}",
                    path,
                ):
                    _, _, _, source_id, local_id = path.split("/")
                    try:
                        payload = source_details(
                            root,
                            source_id,
                            local_id,
                            history_roots=history_roots,
                            records_root=records_root,
                        )
                    except ValueError:
                        self.send_error(404)
                        return
                    body = json.dumps(payload, ensure_ascii=False).encode()
                    mime = "application/json; charset=utf-8"
                elif re.fullmatch(r"/api/node/[0-9]{4,}", path):
                    body = json.dumps(
                        details(root, path.rsplit("/", 1)[1]), ensure_ascii=False
                    ).encode()
                    mime = "application/json; charset=utf-8"
                elif path == "/api/research":
                    body = json.dumps(
                        {"text": prose(root / "study/research.md")}, ensure_ascii=False
                    ).encode()
                    mime = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError, KeyError) as error:
                self.send_error(503, type(error).__name__)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Research dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot-only", action="store_true")
    parser.add_argument("--history-root", type=Path, action="append", default=[])
    parser.add_argument("--records-root", type=Path)
    args = parser.parse_args()
    serve(
        args.root.resolve(),
        args.port,
        snapshot_only=args.snapshot_only,
        history_roots=[path.resolve() for path in args.history_root],
        records_root=args.records_root.resolve() if args.records_root else None,
    )
