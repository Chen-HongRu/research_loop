"""Complete a checkpointed sample under the collector's single-writer lock."""

import json
import os


def _atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, separators=(",", ":"), ensure_ascii=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def publish_pending(output, state, history_for):
    """Finish normal publication or replay the same pending sample after restart.

    The caller has checkpointed pending_sample, source offsets and sample_offset
    before entering. It supplies its own sample enrichment/history projection;
    this module knows only the journal position and the three output files.
    """
    pending = state.get("pending_sample")
    if pending is None:
        return
    history = history_for(pending)
    fd = os.open(output / "samples.jsonl", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if stream.seek(0, os.SEEK_END) < state["sample_offset"]:
            raise ValueError("sample journal is shorter than committed checkpoint")
        stream.truncate(state["sample_offset"])
        stream.seek(state["sample_offset"])
        stream.write(
            (
                json.dumps(history, separators=(",", ":"), ensure_ascii=False) + "\n"
            ).encode()
        )
        stream.flush()
        os.fsync(stream.fileno())
        state["sample_offset"] = stream.tell()
    _atomic(output / "latest.json", pending)
    del state["pending_sample"]
    _atomic(output / "checkpoint.json", state)
