"""Load the bounded declarative specialist strategy."""

import json
from pathlib import Path

MAX_BYTES = 16 * 1024
STAGES = ("plan", "prepare", "evaluate")
DEFAULT_PATH = Path(__file__).with_name("strategy.json")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate strategy field: {key}")
        result[key] = value
    return result


def _fields(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"Invalid {name} fields")


def _text(value, name):
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > MAX_BYTES
    ):
        raise ValueError(f"Invalid {name}")


def load_policy(path=None):
    """Read and strictly validate a small version-1 strategy JSON document."""
    with Path(path if path is not None else DEFAULT_PATH).open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Strategy file exceeds size limit")
    try:
        policy = json.loads(content, object_pairs_hook=_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid strategy JSON") from error
    _fields(policy, ("version", "common", "stages"), "strategy")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise ValueError("Invalid strategy version")
    _text(policy["common"], "common")
    _fields(policy["stages"], STAGES, "stages")
    for stage in STAGES:
        entry = policy["stages"][stage]
        _fields(entry, ("instructions", "context_mode"), stage)
        _text(entry["instructions"], f"{stage} instructions")
        if entry["context_mode"] not in ("paths", "inline"):
            raise ValueError(f"Invalid {stage} context_mode")
    return policy
