"""Dataset preparation and occupied-GPU refusal never start models/training."""

import asyncio
import json
from pathlib import Path

from benchmarks.experiment_cycle import collection


def deployment(tmp_path):
    (tmp_path / "starting.py").write_text(
        "from torch import nn\ndef tiny_cnn():\n return nn.Sequential(nn.ReLU())\n"
    )
    (tmp_path / "metadata.json").write_text("{}")
    config = {
        "starting_source": "starting.py",
        "data_metadata": "metadata.json",
        "remote": {
            "ssh_host": "example-host",
            "project_root": "/path/to/project",
            "data_root": "/path/to/public/data",
            "controller_root": "/path/to/private/controller",
            "python": "/path/to/python",
            "gpu_uuid": "GPU-example",
        },
    }
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(config))
    return path


def test_prepare_ten_distinct_cycles_without_dispatch(tmp_path, monkeypatch):
    async def no_remote(*args, **kwargs):
        raise AssertionError("prepare must not dispatch")

    monkeypatch.setattr(collection, "remote", no_remote)
    result = collection.prepare(deployment(tmp_path), tmp_path / "cases")
    spec = json.loads(Path(result["manifest"]).read_text())
    assert result["status"] == "prepared-not-started"
    assert spec["model"] == "gpt-6.1-terra"
    assert len(spec["cases"]) == spec["target_cycles"] == 10
    tasks = [
        (tmp_path / "cases" / case).parent.joinpath("task.md").read_text()
        for case in spec["cases"]
    ]
    assert len(set(tasks)) == 10
    assert all("one epoch" in task and "BCEWithLogitsLoss" in task for task in tasks)


def test_occupied_card_stops_before_model_or_output(tmp_path, monkeypatch):
    collection.prepare(deployment(tmp_path), tmp_path / "cases")
    calls = []

    async def busy(config, action, *args, **kwargs):
        calls.append(action)
        return {"ready": False, "reason": "occupied"}

    async def no_model(*args, **kwargs):
        raise AssertionError("occupied GPU must be checked before model dispatch")

    monkeypatch.setattr(collection, "remote", busy)
    monkeypatch.setattr(collection, "run", no_model)
    output = tmp_path / "runs"
    result = asyncio.run(collection.collect(tmp_path / "cases", output))
    assert result == {
        "status": "blocked",
        "reason": "occupied",
        "model_calls": 0,
        "cycles_completed": 0,
    }
    assert calls == ["preflight"]
    assert not output.exists()
