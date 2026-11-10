"""Shared acceptance of recorded request usage; cumulative counts are separate."""

TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
AUTHORITY = "unique response_id token_usage_record"


def cumulative_counts(info):
    """Return a cumulative snapshot, or None without resetting the prior baseline."""
    value = info.get("total_token_usage") if isinstance(info, dict) else None
    if (
        not isinstance(value, dict)
        or not any(k in value for k in TOKEN_KEYS)
        or any(
            type(value.get(k, 0)) is not int or value.get(k, 0) < 0 for k in TOKEN_KEYS
        )
    ):
        return None
    return {k: value.get(k, 0) for k in TOKEN_KEYS}


def accept_record(payload, seen):
    """Validate and deduplicate Study-wide response IDs, including before a window.

    Return (usage, issue). Duplicates have issue='duplicate'; rejected receipts do
    not reserve an ID, so a later valid receipt can still be accepted. The caller
    applies the notification-time window and persists seen IDs when incremental.
    """
    rid, value = payload.get("response_id"), payload.get("usage")
    if not isinstance(rid, str) or not rid:
        return None, "missing_response_id"
    if rid in seen:
        return None, "duplicate"
    if (
        not isinstance(value, dict)
        or not any(k in value for k in TOKEN_KEYS)
        or any(
            type(value.get(k, 0)) is not int or value.get(k, 0) < 0 for k in TOKEN_KEYS
        )
        or value.get("cached_input_tokens", 0) > value.get("input_tokens", 0)
    ):
        return None, "invalid_usage_record"
    seen.add(rid)
    return {**{k: value.get(k, 0) for k in TOKEN_KEYS}, "responses": 1}, None
