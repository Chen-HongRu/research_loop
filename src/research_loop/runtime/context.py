"""Immutable, controller-supplied context and auditable field selection.

Policies receive detached ``(eligible_inputs, eligible_sources, budget)`` arguments and
return key-to-selection mappings. They cannot grant evidence access or rewrite
content. No artifact is read here; source metadata is trusted controller input.
Source revisions are opaque finite JSON values supplied by that controller.
The budget is selection metadata, not a universally enforced token ceiling.
"""

from dataclasses import dataclass
import json
import math
from typing import Callable


_FIXED_KEYS = (
    "goal",
    "plan",
    "global_evaluation",
    "permissions",
    "budget",
    "manual_task",
    "manual_task_constraints",
)
_SOURCE_KEYS = {"id", "key", "ref", "revision", "reason", "allow_feedback"}


def _json(value):
    def validate(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is None or type(item) in (str, bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ValueError("Expected finite JSON values")

    validate(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")


def _nonblank(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")


def _strings(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(f"{name} must be a list of strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must be unique")


def _sources(sources):
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    ids, keys = set(), set()
    for source in sources:
        _object(source, "source")
        if not _SOURCE_KEYS <= source.keys():
            raise ValueError("Missing source metadata")
        for field in ("id", "key", "ref", "reason"):
            _nonblank(source[field], f"source {field}")
        if type(source["allow_feedback"]) is not bool:
            raise ValueError("allow_feedback must be a boolean")
        if source["id"] in ids or source["key"] in keys:
            raise ValueError("source ids and keys must be unique")
        ids.add(source["id"])
        keys.add(source["key"])
    return keys


def _access(sources, protected, allowed):
    _strings(protected, "protected_keys")
    _strings(allowed, "allowed_evidence")
    eligible_refs = {s["ref"] for s in sources if s["allow_feedback"]}
    if not set(allowed) <= eligible_refs:
        raise ValueError("allowed_evidence contains an ineligible or unknown ref")
    if any(not s["allow_feedback"] and s["key"] in protected for s in sources):
        raise ValueError("Protected fields must be eligible for feedback")


@dataclass(frozen=True, init=False)
class ContextSnapshot:
    """A captured JSON snapshot; returned data never aliases stored state."""

    _serialized: str

    @classmethod
    def capture(
        cls, *, study_id, revisions, inputs, sources, protected_keys, allowed_evidence
    ):
        value = dict(
            study_id=study_id,
            revisions=revisions,
            inputs=inputs,
            sources=sources,
            protected_keys=protected_keys,
            allowed_evidence=allowed_evidence,
        )
        serialized = _json(value)
        _nonblank(study_id, "study_id")
        _object(revisions, "revisions")
        _object(inputs, "inputs")
        keys = _sources(sources)
        if any(set(source) != _SOURCE_KEYS for source in sources):
            raise ValueError("Unknown source metadata")
        if keys != inputs.keys():
            raise ValueError("Exactly one source is required per input key")
        _access(sources, protected_keys, allowed_evidence)
        if not set(protected_keys) <= keys:
            raise ValueError("Unknown protected key")
        fixed = set(_FIXED_KEYS) & keys
        _access(sources, list(fixed | set(protected_keys)), allowed_evidence)
        instance = object.__new__(cls)
        object.__setattr__(instance, "_serialized", serialized)
        return instance

    def to_dict(self):
        return json.loads(self._serialized)


@dataclass(frozen=True, init=False)
class ContextPackage:
    _serialized: str

    @classmethod
    def from_dict(cls, value):
        serialized = _json(value)
        _object(value, "package")
        if set(value) != {"context", "manifest"}:
            raise ValueError("Expected context and manifest")
        context, manifest = value["context"], value["manifest"]
        _object(context, "context")
        _object(manifest, "manifest")
        expected = {
            "version",
            "study_id",
            "role",
            "task",
            "revisions",
            "policy_ref",
            "budget",
            "protected_keys",
            "allowed_evidence",
            "sources",
            "tokens",
        }
        if (
            set(manifest) != expected
            or type(manifest["version"]) is not int
            or manifest["version"] != 1
        ):
            raise ValueError("Unsupported context manifest")
        for field in ("study_id", "role", "policy_ref"):
            _nonblank(manifest[field], field)
        for field in ("revisions", "budget"):
            _object(manifest[field], field)
        if manifest["task"] is not None:
            _object(manifest["task"], "task")
        sources = manifest["sources"]
        keys = _sources(sources)
        protected = manifest["protected_keys"]
        _access(sources, protected, manifest["allowed_evidence"])
        if not set(protected) <= keys or not set(_FIXED_KEYS) & keys <= set(protected):
            raise ValueError("Invalid protected keys")
        provided = set()
        for source in sources:
            required = _SOURCE_KEYS | {"disposition", "selection_reason"}
            counts = {"original_characters", "provided_characters"}
            if set(source) not in (required, required | counts):
                raise ValueError("Invalid manifest source metadata")
            action = source["disposition"]
            if action not in ("provided", "omitted", "truncated") or not isinstance(
                source["selection_reason"], str
            ):
                raise ValueError("Invalid source selection")
            key = source["key"]
            if (not source["allow_feedback"] and action != "omitted") or (
                key in protected and action != "provided"
            ):
                raise ValueError("Invalid protected or denied source selection")
            if action != "omitted":
                provided.add(key)
            if counts <= source.keys():
                original, supplied = (
                    source["original_characters"],
                    source["provided_characters"],
                )
                if (
                    type(original) is not int
                    or type(supplied) is not int
                    or not 0 <= supplied <= original
                ):
                    raise ValueError("Invalid character counts")
                if action == "omitted" and supplied != 0:
                    raise ValueError("Omitted source cannot provide characters")
                if action != "omitted" and (
                    not isinstance(context.get(key), str)
                    or len(context[key]) != supplied
                ):
                    raise ValueError("Character count disagrees with context")
                if action == "provided" and supplied != original:
                    raise ValueError("Provided source must be unchanged")
            elif action == "truncated" or isinstance(context.get(key), str):
                raise ValueError("Text sources require character counts")
        if context.keys() != provided:
            raise ValueError("Context keys disagree with manifest")
        expected_tokens = {
            "estimated_input_tokens": math.ceil(len(_json(context)) / 4),
            "estimate_method": "characters/4",
            "actual_usage": None,
        }
        if manifest["tokens"] != expected_tokens:
            raise ValueError("Invalid token estimate or invented actual usage")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_serialized", serialized)
        return instance

    @property
    def context(self):
        return self.to_dict()["context"]

    @property
    def manifest(self):
        return self.to_dict()["manifest"]

    def to_dict(self):
        return json.loads(self._serialized)


def build_context(
    snapshot: ContextSnapshot,
    role: str,
    task: dict | None,
    policy_ref: str,
    budget: dict,
    *,
    policy: Callable | None = None,
    visible_evidence_only: bool = False,
):
    """Select whole fields or string prefixes; estimate serialized JSON tokens."""
    captured = snapshot.to_dict()
    inputs, sources = captured["inputs"], captured["sources"]
    eligible = {s["key"] for s in sources if s["allow_feedback"]}
    protected = [key for key in _FIXED_KEYS if key in inputs]
    protected += [key for key in captured["protected_keys"] if key not in protected]
    selections = {}
    if policy is not None:
        detached_inputs = {k: v for k, v in inputs.items() if k in eligible}
        detached_sources = [s for s in sources if s["allow_feedback"]]
        selections = policy(
            json.loads(_json(detached_inputs)),
            json.loads(_json(detached_sources)),
            json.loads(_json(budget)),
        )
        _json(selections)
        _object(selections, "policy selections")
        if not selections.keys() <= eligible:
            raise ValueError("Policy selected an unknown or denied field")
    context, recorded = {}, []
    for source in sources:
        key = source["key"]
        selected = selections.get(
            key, {"action": "include", "reason": "default include"}
        )
        _object(selected, "selection")
        action = selected.get("action")
        fields = (
            {"action", "reason", "characters"}
            if action == "truncate"
            else {"action", "reason"}
        )
        if (
            set(selected) != fields
            or action not in ("include", "omit", "truncate")
            or not isinstance(selected["reason"], str)
        ):
            raise ValueError("Invalid policy selection")
        if key in protected and action != "include":
            raise ValueError("Policy cannot remove or truncate protected fields")
        value = inputs[key]
        reason = selected["reason"]
        if key not in eligible:
            action, reason = "omit", "not eligible for feedback"
        if action == "truncate":
            count = selected["characters"]
            if not isinstance(value, str) or type(count) is not int or count < 0:
                raise ValueError(
                    "Truncation requires a string and nonnegative character count"
                )
            value = value[:count]
        if action != "omit":
            context[key] = value
        item = dict(
            source,
            disposition={
                "include": "provided",
                "omit": "omitted",
                "truncate": "truncated",
            }[action],
            selection_reason=reason,
        )
        if isinstance(inputs[key], str):
            item.update(
                original_characters=len(inputs[key]),
                provided_characters=0 if action == "omit" else len(value),
            )
        recorded.append(item)
    # Source order is metadata order; preserve the original input order for prompts.
    context = {key: context[key] for key in inputs if key in context}
    allowed = captured["allowed_evidence"]
    if visible_evidence_only:
        visible = {s["ref"] for s in recorded if s["disposition"] != "omitted"}
        allowed = [ref for ref in allowed if ref in visible]
    manifest = dict(
        version=1,
        study_id=captured["study_id"],
        role=role,
        task=task,
        revisions=captured["revisions"],
        policy_ref=policy_ref,
        budget=budget,
        protected_keys=protected,
        allowed_evidence=allowed,
        sources=recorded,
        tokens={
            "estimated_input_tokens": math.ceil(len(_json(context)) / 4),
            "estimate_method": "characters/4",
            "actual_usage": None,
        },
    )
    return ContextPackage.from_dict({"context": context, "manifest": manifest})
