"""Declarative strategy loading has a deliberately narrow surface."""

import json

import pytest

from benchmarks.experiment_cycle.policy import MAX_BYTES, load_policy


def test_default_and_real_file(tmp_path):
    policy = load_policy()
    assert set(policy["stages"]) == {"plan", "prepare", "evaluate"}
    assert all(item["context_mode"] == "paths" for item in policy["stages"].values())
    policy["stages"]["plan"]["context_mode"] = "inline"
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(policy))
    assert load_policy(path) == policy


@pytest.mark.parametrize("field", ["command", "model", "oracle"])
def test_rejects_extra_fields(tmp_path, field):
    policy = load_policy()
    policy["stages"]["prepare"][field] = "override"
    path = tmp_path / "strategy.json"
    path.write_text(json.dumps(policy))
    with pytest.raises(ValueError):
        load_policy(path)


@pytest.mark.parametrize(
    "mutation", ["version", "stage", "mode", "empty", "nul", "duplicate", "large"]
)
def test_rejects_invalid_strategy(tmp_path, mutation):
    policy = load_policy()
    if mutation == "version":
        policy["version"] = True
    elif mutation == "stage":
        policy["stages"]["execute"] = policy["stages"]["plan"]
    elif mutation == "mode":
        policy["stages"]["plan"]["context_mode"] = "shell"
    elif mutation == "empty":
        policy["common"] = "  "
    elif mutation == "nul":
        policy["common"] = "invalid\0text"
    text = json.dumps(policy)
    if mutation == "duplicate":
        text = text.replace('"version": 1', '"version": 1, "version": 1')
    elif mutation == "large":
        text = " " * (MAX_BYTES + 1)
    path = tmp_path / "strategy.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_policy(path)
