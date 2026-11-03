"""CPU-only, incremental all-tag telemetry mirror and local TensorBoard service.

Only the supplied derived logdir is written. Source studies remain read-only.
Each bounded page is journaled before publishing immutable event files, so a
restart can finish a partially published page without duplicating observations.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research_loop import telemetry
from integrations.protok.task_telemetry import manual_locations

TRANSPORT_KEYS = {
    "remote_start_offset",
    "remote_end_offset",
    "raw_fragment_bytes",
    "raw_fragment_complete",
    "csv_discard_until_newline",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def identity(value):
    # Configuration/provenance only; source artifact contents are never hashed.
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(canonical(value))
    temporary.replace(path)


def scalar_values(value):
    """Small JSON objects expose numeric leaf fields without flattening arrays."""
    if type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError:
            return
        if math.isfinite(number):
            yield "", number
    elif isinstance(value, dict):
        for key, item in sorted(value.items()):
            for suffix, number in scalar_values(item):
                # Escape field separators so a literal key cannot merge curves.
                yield "/" + key.replace("~", "~0").replace("/", "~1") + suffix, number


def summaries(event, source, stream_id):
    from tensorboard.compat.proto import summary_pb2, tensor_pb2, types_pb2
    from tensorboard.plugins.text import summary_v2

    # Shared task runs retain exact stream provenance for nonnumeric evidence too.
    evidence_id = identity({"stream": stream_id, "source": source})[:16]
    result = summary_v2.text_pb(
        "evidence/" + event["name"] + " [" + stream_id + "; " + evidence_id + "]",
        canonical({**event, "stream_id": stream_id, "source": source}),
    )
    conditions = event["context"]
    if source["kind"] == "fixed-short":
        conditions = {k: v for k, v in conditions.items() if k not in TRANSPORT_KEYS}
    axis = "step" if event["step"] is not None else "event-order"
    contract = {
        "name": event["name"],
        "unit": event["unit"],
        "conditions": conditions,
        "axis": axis,
        "stream": stream_id,
        "source": source,
    }
    # Conditions belong to a series, not to the experiment's run selector.
    readable = ", ".join(
        f"{key}={canonical(value)}"
        for key, value in sorted(conditions.items())
        if key not in {"source", "csv_header"}
    )[:80]
    details = "; ".join(part for part in (readable, event["unit"], axis) if part)
    for suffix, value in scalar_values(event["value"]):
        result.value.add(
            tag="observations/"
            + event["name"]
            + suffix
            + " ["
            + details
            + "; "
            + identity(contract)[:16]
            + "]",
            tensor=tensor_pb2.TensorProto(
                dtype=types_pb2.DT_DOUBLE, double_val=[value]
            ),
            metadata=summary_pb2.SummaryMetadata(
                display_name=event["name"] + suffix + " [" + details + "]",
                summary_description=canonical(contract),
                plugin_data=summary_pb2.SummaryMetadata.PluginData(
                    plugin_name="scalars"
                ),
            ),
        )
    return result


def task_name(source_id, lane, source):
    if lane and lane.startswith("manual:"):
        _, task_id, iteration = lane.split(":")
        name = "manual/" + task_id + "/#" + iteration
    elif lane:
        name = "long/" + lane
    else:
        name = "#" + quote(source["experiment_id"], safe="")
    return source_id + "/" + name


class Exporter:
    def __init__(self, root, logdir, history_roots=()):
        self.sources = [
            ("active", Path(root).resolve()),
            *[
                (f"history-{index}", Path(path).resolve())
                for index, path in enumerate(history_roots, 1)
            ],
        ]
        self.logdir = Path(logdir).resolve()
        # Reject writes into a source Study, or a derived dir containing sources.
        for _, source_root in self.sources:
            study = source_root / "study"
            if self.logdir.is_relative_to(study) or source_root.is_relative_to(
                self.logdir
            ):
                raise ValueError("Derived logdir must be separate from source Studies")
        # Rebuild only this derived layout; keep the previous mirror intact.
        self.state_dir = self.logdir / ".export-state-v2"
        self.events_dir = self.logdir / "events-v2"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def locked(self):
        with (self.state_dir / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.layout_path = self.state_dir / "layout.json"
            sources = [[key, str(root)] for key, root in self.sources]
            self.layout = (
                json.loads(self.layout_path.read_text())
                if self.layout_path.exists()
                else {"sources": sources, "executions": {}, "next_page": 0}
            )
            if self.layout["sources"] != sources:
                raise ValueError("Derived logdir belongs to different source Studies")
            yield

    def page_identity(self, source_id, lane, source):
        task = task_name(source_id, lane, source)
        execution = [
            source[key] for key in ("kind", "source_identity", "execution_identity")
        ]
        attempts = self.layout["executions"].setdefault(task, [])
        if execution not in attempts:
            attempts.append(execution)
        attempt = attempts.index(execution) + 1
        run = task if attempt == 1 else f"{task} (attempt {attempt})"
        sequence = self.layout["next_page"]
        self.layout["next_page"] += 1
        atomic_json(self.layout_path, self.layout)
        return run, sequence

    def streams(self):
        for source_id, root in self.sources:
            study = root / "study"
            locations = [(None, study)]
            locations.extend(
                (folder.name, folder / "study")
                for folder in sorted((study / "long-runs").glob("L*"))
                if re.fullmatch(r"L[0-9a-f]{16}", folder.name)
                and folder.is_dir()
                and not folder.is_symlink()
            )
            locations.extend(
                ("manual:" + task_id + ":" + iteration, location)
                for task_id, iteration, location in manual_locations(study)
            )
            for lane, location in locations:
                for folder in sorted((location / "telemetry").glob("*")):
                    if folder.is_dir() and not folder.is_symlink():
                        yield source_id, root, lane, location, folder.name

    def publish(self, pending):
        from tensorboard.compat.proto import event_pb2
        from tensorboard.summary.writer.event_file_writer import EventFileWriter

        grouped = defaultdict(list)
        for index, event in enumerate(pending["page"]["events"], 1):
            run = pending["run"]
            step = event["step"]
            if step is None:
                step = pending["order"] + index
            if not -(2**63) <= step < 2**63:
                # Protobuf steps are int64. Preserve the original integer in text.
                run += "/step-out-of-int64-event-order"
                step = pending["order"] + index
            grouped[run].append(
                event_pb2.Event(
                    wall_time=event["time"],
                    step=step,
                    summary=summaries(event, pending["source"], pending["stream_id"]),
                )
            )
        filename = f"events.out.tfevents.{pending['sequence']:020d}"
        for run, events in grouped.items():
            target = self.events_dir / run / filename
            if target.exists():
                continue
            # Staging is outside TensorBoard's watched events directory.
            with tempfile.TemporaryDirectory(dir=self.state_dir) as temporary:
                writer = EventFileWriter(temporary)
                try:
                    for event in events:
                        writer.add_event(event)
                    writer.flush()
                finally:
                    writer.close()
                staged = next(Path(temporary).glob("events.out.tfevents.*"))
                target.parent.mkdir(parents=True, exist_ok=True)
                staged.replace(target)

    def export_stream(self, source_id, root, lane, location, stream_id):
        current = telemetry.info(location, stream_id)
        source = current["source"]
        key = identity([source_id, str(root), lane, stream_id, source])
        state_path = self.state_dir / (key + ".json")
        pending_path = self.state_dir / (key + ".pending")
        state = (
            json.loads(state_path.read_text())
            if state_path.exists()
            else {"offset": 0, "order": 0, "source": source}
        )
        if state["source"] != source:
            raise ValueError("Telemetry source identity changed")
        if pending_path.exists():
            pending = json.loads(pending_path.read_text())
        else:
            page = telemetry.read_events(location, stream_id, after=state["offset"])
            if page["next_offset"] == state["offset"]:
                return 0
            run, sequence = self.page_identity(source_id, lane, source)
            pending = {
                "source_id": source_id,
                "root": str(root),
                "lane": lane,
                "stream_id": stream_id,
                "source": source,
                "order": state["order"],
                "page": page,
                "run": run,
                "sequence": sequence,
            }
            atomic_json(pending_path, pending)
        return self.finish_page(pending_path, pending)

    def finish_page(self, pending_path, pending):
        self.publish(pending)
        page = pending["page"]
        atomic_json(
            pending_path.with_suffix(".json"),
            {
                "offset": page["next_offset"],
                "order": pending["order"] + len(page["events"]),
                "source": pending["source"],
                "errors": page["errors"],
            },
        )
        pending_path.unlink()
        return len(page["events"])

    def poll(self):
        """One bounded page per stream; bad streams cannot stop healthy streams."""
        count, errors = 0, []
        with self.locked():
            # TensorBoard tails event files in filename order. Complete an older
            # transaction before publishing newer pages into a shared task run.
            pending = [
                (path, json.loads(path.read_text()))
                for path in self.state_dir.glob("*.pending")
            ]
            if pending:
                for path, page in sorted(pending, key=lambda item: item[1]["sequence"]):
                    try:
                        count += self.finish_page(path, page)
                    except Exception as exc:
                        errors.append(f"Pending page: {type(exc).__name__}")
                        break
                return {"exported": count, "errors": errors}
            for source_id, root, lane, location, stream_id in self.streams():
                try:
                    count += self.export_stream(
                        source_id, root, lane, location, stream_id
                    )
                except Exception as exc:
                    errors.append(
                        f"{source_id}/{lane or 'main'}/{stream_id}: {type(exc).__name__}"
                    )
                    if any(self.state_dir.glob("*.pending")):
                        break
        return {"exported": count, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--history-root", type=Path, action="append", default=[])
    parser.add_argument("--logdir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=6006)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be positive and finite")
    try:
        from tensorboard import program
    except ImportError:
        parser.error("Install the dashboard extra: uv sync --extra dashboard")
    exporter = Exporter(args.root, args.logdir, args.history_root)
    result = exporter.poll()
    print(canonical(result), flush=True)
    if args.once:
        return
    from integrations.protok.tensorboard_plugins import task_plugins

    board = program.TensorBoard(
        plugins=task_plugins({source_id for source_id, _ in exporter.sources})
    )
    board.configure(
        argv=[
            "tensorboard",
            "--logdir",
            str(exporter.events_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(args.port),
            "--path_prefix",
            "/tensorboard",
            "--reload_interval",
            str(args.interval),
            "--load_fast",
            "false",
        ]
    )
    print(board.launch(), flush=True)
    try:
        while True:
            time.sleep(args.interval)
            result = exporter.poll()
            if result["errors"]:
                print(canonical(result), file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
