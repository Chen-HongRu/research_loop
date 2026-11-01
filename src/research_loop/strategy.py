"""Small policy choices; protocol validation and publication stay in the controller."""

from __future__ import annotations


def validate_strategy(strategy: dict | None = None) -> dict:
    if strategy is None:
        return {"selection": "debate", "review": "full"}
    if not isinstance(strategy, dict) or set(strategy) != {"selection", "review"}:
        raise ValueError("Strategy requires exactly selection and review")
    if strategy["selection"] not in ("debate", "direct"):
        raise ValueError("Strategy selection must be debate or direct")
    if strategy["review"] not in ("full", "concise"):
        raise ValueError("Strategy review must be full or concise")
    return dict(strategy)


def debate_rounds(strategy: dict, configured_rounds: int) -> int:
    return configured_rounds if strategy["selection"] == "debate" else 0


def review_guidance(strategy: dict) -> str:
    if strategy["review"] == "concise":
        return (
            "Keep the Review concise: cite the provided Plan, execution measurements "
            "and actual evidence instead of repeating their prose. Verify every "
            "candidate and retain evidence conflicts, limitations, knowledge changes, "
            "and complete route and global research updates. Do not omit contract "
            "checks or required response fields."
        )
    return ""
