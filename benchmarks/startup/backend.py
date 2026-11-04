"""Fixed one-job ExecutionBackend with operator setup and retained effect identity."""

from collections.abc import Callable
import json
import os
from pathlib import Path

from research_loop.execution import ExecutionBackend


def _save(path: Path, value: dict, *, exclusive: bool = False) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("x") as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    if exclusive:
        try:
            os.link(temporary, path)
        finally:
            temporary.unlink()
    else:
        temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FixedStartupBackend(ExecutionBackend[dict, dict]):
    def __init__(
        self,
        *,
        run_id: str,
        state_dir: Path,
        source_identity: dict,
        prepare_remote: Callable[[], dict],
        run_gpu: Callable[[], dict],
    ):
        self.run_id = run_id
        self.directory = Path(state_dir)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.source_identity = dict(source_identity)
        self.prepare_remote = prepare_remote
        self.run_gpu = run_gpu

    def setup(self) -> dict:
        """Operator staging may create an environment; execution prepare never does."""
        path = self.directory / "setup.json"
        if path.exists():
            retained = json.loads(path.read_text())
            if (
                retained.get("status") != "prepared"
                or retained.get("run_id") != self.run_id
                or retained.get("source_identity") != self.source_identity
            ):
                raise RuntimeError(
                    "Setup effects unresolved; inspect retained identity"
                )
            return retained
        _save(path, {"status": "accepted", "run_id": self.run_id}, exclusive=True)
        manifest = json.loads(json.dumps(self.prepare_remote(), allow_nan=False))
        if (
            type(manifest) is not dict
            or manifest.get("status") != "prepared"
            or manifest.get("run_id") != self.run_id
        ):
            raise ValueError("Remote setup did not prove the configured case identity")
        result = {**manifest, "source_identity": self.source_identity}
        _save(path, result)
        return result

    def prepare(self) -> dict:
        """Pure fixed-contract validation; no environment staging or GPU allocation."""
        if any(
            not isinstance(self.source_identity.get(field), str)
            or not self.source_identity[field]
            for field in ("baseline_commit", "source_tree")
        ):
            raise ValueError("Configured source identity is incomplete")
        manifest = json.loads((self.directory / "setup.json").read_text())
        if (
            manifest.get("status") != "prepared"
            or manifest.get("run_id") != self.run_id
            or manifest.get("source_identity") != self.source_identity
        ):
            raise ValueError("Setup identity does not match the fixed source")
        return {
            "run_id": self.run_id,
            "source_identity": self.source_identity,
            "seed": 43,
            "updates": 3,
            "synthetic": True,
        }

    def start(self, prepared: dict) -> dict:
        if prepared != self.prepare():
            raise ValueError("Prepared execution does not match the fixed contract")
        path = self.directory / "execution.json"
        if path.exists():
            raise RuntimeError(
                "Execution identity already retained; inspect, never retry"
            )
        accepted = {
            "status": "accepted",
            "execution_identity": self.run_id,
            "prepared": prepared,
        }
        _save(path, accepted, exclusive=True)
        # Any callback failure leaves accepted identity; it never authorizes replay.
        receipt = json.loads(json.dumps(self.run_gpu(), allow_nan=False))
        if (
            type(receipt) is not dict
            or receipt.get("status") != "completed"
            or type(receipt.get("completed_steps")) is not int
            or receipt["completed_steps"] != 3
            or receipt.get("seed") != 43
            or receipt.get("synthetic") is not True
            or not isinstance(receipt.get("device"), str)
            or not (
                receipt["device"] == "cuda" or receipt["device"].startswith("cuda:")
            )
        ):
            raise ValueError("Receipt does not prove the fixed CUDA smoke")
        _save(path, {**accepted, "status": "completed", "receipt": receipt})
        return receipt

    def inspect(self) -> dict:
        path = self.directory / "execution.json"
        if not path.exists():
            return {"status": "not_started", "execution_identity": self.run_id}
        retained = json.loads(path.read_text())
        if retained.get("execution_identity") != self.run_id:
            raise ValueError("Retained execution identity mismatch")
        return retained.get("receipt", retained)
