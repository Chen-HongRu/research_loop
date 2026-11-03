"""Bound dashboard task identities to TensorBoard's run-indexed JSON APIs."""

from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qsl, unquote_plus, urlsplit, urlunsplit

from integrations.protok.task_telemetry import TASK_ID

JSON_ROUTES = frozenset(
    {
        "/data/runs",
        "/data/plugin/scalars/tags",
        "/data/plugin/text/tags",
        "/data/plugin/scalars/scalars_multirun",
        "/data/plugin/timeseries/tags",
        "/data/plugin/timeseries/timeSeries",
    }
)


@dataclass(frozen=True)
class Scope:
    source: str
    experiment: str | None = None
    run: str | None = None
    task: str | None = None

    def matches(self, run_name):
        """Match one canonical run, its attempts, and its overflow-step stream."""
        if not isinstance(run_name, str):
            return False
        if self.task is not None:
            name = re.escape(f"{self.source}/manual/{self.task}/#") + r"[0-9]{4,}"
        elif self.run is not None:
            name = re.escape(f"{self.source}/long/{self.run}")
        else:
            name = re.escape(f"{self.source}/#{self.experiment}")
        return (
            re.fullmatch(
                name
                + r"(?: \(attempt [1-9][0-9]*\))?"
                + r"(?:/step-out-of-int64-event-order)?",
                run_name,
            )
            is not None
        )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate dashboard task field")
        result[key] = value
    return result


def parse_request(path, allowed_sources):
    """Remove the dashboard-only selector and reject out-of-scope run queries."""
    parts = urlsplit(path)
    query = parse_qsl(parts.query, keep_blank_values=True)
    values = [value for key, value in query if key == "dashboard_task"]
    if not values:
        return path, None
    if len(values) != 1 or len(values[0]) > 1024:
        raise ValueError("Invalid dashboard task scope")
    try:
        value = json.loads(values[0], object_pairs_hook=_object)
    except (ValueError, RecursionError) as error:
        raise ValueError("Invalid dashboard task JSON") from error
    if not isinstance(value, dict) or not set(value) <= {
        "source",
        "experiment",
        "run",
        "task",
    }:
        raise ValueError("Invalid dashboard task fields")
    source = value.get("source")
    if (
        not isinstance(source, str)
        or not re.fullmatch(r"active|history-[1-9][0-9]*", source)
        or source not in allowed_sources
    ):
        raise ValueError("Unknown dashboard task source")
    selectors = {
        key: value[key]
        for key in ("experiment", "run", "task")
        if value.get(key) is not None
    }
    if len(selectors) != 1:
        raise ValueError("Select exactly one dashboard task identity")
    key, identity = next(iter(selectors.items()))
    pattern = {"experiment": r"[0-9]{4,}", "run": r"L[0-9a-f]{16}", "task": TASK_ID}[
        key
    ]
    if not isinstance(identity, str) or not re.fullmatch(pattern, identity):
        raise ValueError("Invalid dashboard task identity")
    scope = Scope(source, **selectors)
    if any(key == "run" and not scope.matches(value) for key, value in query):
        raise ValueError("TensorBoard run is outside the dashboard task")
    # Preserve all upstream query bytes, including repeated fields and encoding.
    query_text = "&".join(
        field
        for field in parts.query.split("&")
        if unquote_plus(field.partition("=")[0]) != "dashboard_task"
    )
    return urlunsplit(parts._replace(query=query_text)), scope


def route(path):
    """Normalize TensorBoard's mount and optional experiment route prefix."""
    value = urlsplit(path).path
    if value == "/tensorboard":
        return "/"
    if value.startswith("/tensorboard/"):
        value = value[len("/tensorboard") :]
    return re.sub(r"^/experiment/[^/]*/", "/", value)


def _runs(mapping, scope):
    return {run: value for run, value in mapping.items() if scope.matches(run)}


def _tags(payload, scope):
    result = {}
    for plugin, metadata in payload.items():
        if not isinstance(metadata, dict):
            result[plugin] = metadata
            continue
        filtered = dict(metadata)
        if isinstance(metadata.get("runTagInfo"), dict):
            filtered["runTagInfo"] = _runs(metadata["runTagInfo"], scope)
        if isinstance(metadata.get("tagRunSampledInfo"), dict):
            images = {}
            for tag, runs in metadata["tagRunSampledInfo"].items():
                if isinstance(runs, dict) and (selected := _runs(runs, scope)):
                    images[tag] = selected
            filtered["tagRunSampledInfo"] = images
        if "tagDescriptions" in metadata:
            # TensorBoard combines descriptions across runs before this response.
            # The HTML has no reliable per-run provenance, even for retained tags.
            filtered["tagDescriptions"] = {}
        result[plugin] = filtered
    return result


def filter_response(normalized_route, payload, scope):
    """Filter recognized run-indexed responses without rewriting scientific tags."""
    if scope is None:
        return payload
    if normalized_route == "/data/runs" and isinstance(payload, list):
        return [run for run in payload if scope.matches(run)]
    if normalized_route in {
        "/data/plugin/scalars/tags",
        "/data/plugin/text/tags",
        "/data/plugin/scalars/scalars_multirun",
    } and isinstance(payload, dict):
        return _runs(payload, scope)
    if normalized_route == "/data/plugin/timeseries/tags" and isinstance(payload, dict):
        return _tags(payload, scope)
    if normalized_route == "/data/plugin/timeseries/timeSeries" and isinstance(
        payload, list
    ):
        result = []
        for series in payload:
            if not isinstance(series, dict):
                result.append(series)
                continue
            if isinstance(series.get("run"), str) and not scope.matches(series["run"]):
                # TensorBoard reads responses by request position, including
                # stale pinned cards. Keep the slot while withholding its data.
                result.append({**series, "runToSeries": {}})
                continue
            value = dict(series)
            if isinstance(series.get("runToSeries"), dict):
                value["runToSeries"] = _runs(series["runToSeries"], scope)
            result.append(value)
        return result
    return payload
