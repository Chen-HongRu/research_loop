"""Read-only public iteration history; no model or external services."""

import json

from research_loop.dashboard.server import research_history


def write_record(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def test_history_orders_numerically_and_marks_unfinished_iterations(tmp_path):
    for number, state in ((10, "running"), (2, "iteration-completed"), (1, "failed")):
        write_record(
            tmp_path,
            f"iterations/{number}/status.json",
            {
                "iteration": number,
                "status": state,
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:01:00Z" if number != 10 else None,
                "result": {"summary": "PRIVATE_EMBEDDED_RESULT"},
                "error": {"message": "PRIVATE_ERROR"},
            },
        )
    write_record(
        tmp_path,
        "iterations/2/result.json",
        {
            "summary": "Observed result",
            "next_action": "Test another hypothesis",
            "goal": "unknown",
            "limitations": ["Small sample", {"text": "PRIVATE_NESTED"}, 12],
            "study_action": "continue",
            "stop_reason": "",
            "evidence": [
                "work/report.md#finding",
                "gAAAAAencrypted",
                {"text": "PRIVATE"},
            ],
            "reasoning": "PRIVATE_REASONING",
            "transcript": "PRIVATE_TRANSCRIPT",
        },
    )
    write_record(tmp_path, "result.json", {"summary": "STALE_ROOT_RESULT"})
    history = research_history(tmp_path)
    assert [row["iteration"] for row in history["iterations"]] == [1, 2, 10]
    assert history["counts"] == {"total": 3, "with_result": 1, "missing_result": 2}
    failed, completed, running = history["iterations"]
    assert failed["status"] == "failed" and failed["missing_result"]
    assert failed["result"] is None
    assert completed["result"] == {
        "summary": "Observed result",
        "next_action": "Test another hypothesis",
        "goal": "unknown",
        "limitations": ["Small sample"],
        "study_action": "continue",
        "stop_reason": "",
        "evidence_refs": ["work/report.md#finding"],
    }
    assert (
        completed["source"] == completed["result_source"] == "iterations/2/result.json"
    )
    assert completed["status_source"] == "iterations/2/status.json"
    assert completed["started_at"] == "2026-01-01T00:00:00Z"
    assert completed["ended_at"] == "2026-01-01T00:01:00Z"
    assert running["ended_at"] is None and running["missing_result"]
    assert running["source"] == "iterations/10/status.json"
    assert "PRIVATE" not in json.dumps(history)
    assert "STALE_ROOT_RESULT" not in json.dumps(history)


def test_history_requires_an_iteration_archive(tmp_path):
    write_record(tmp_path, "status.json", {"status": "completed"})
    write_record(tmp_path, "result.json", {"summary": "Root result is not an archive"})
    assert research_history(tmp_path)["iterations"] == []
    write_record(tmp_path, "iterations/000002/status.json", {"status": "running"})
    assert [row["iteration"] for row in research_history(tmp_path)["iterations"]] == [2]


def test_history_handles_result_only_and_malformed_records(tmp_path):
    write_record(tmp_path, "iterations/1/result.json", {"summary": "gAAAAAencrypted"})
    path = write_record(tmp_path, "iterations/2/result.json", [])
    path.write_text("{")
    write_record(tmp_path, "iterations/2/status.json", {"status": "failed"})
    write_record(tmp_path, "iterations/3/status.json", [])
    write_record(
        tmp_path, "iterations/not-a-number/result.json", {"summary": "IGNORED"}
    )
    history = research_history(tmp_path)
    assert [row["iteration"] for row in history["iterations"]] == [1, 2]
    assert history["iterations"][0]["status"] == "unknown"
    assert history["iterations"][0]["result"] == {"summary": ""}
    assert history["iterations"][1]["missing_result"]


def test_history_rejects_symlink_directories_and_files(tmp_path):
    outside = tmp_path / "outside"
    write_record(outside, "status.json", {"status": "PRIVATE"})
    write_record(outside, "result.json", {"summary": "PRIVATE"})
    iterations = tmp_path / "iterations"
    iterations.mkdir()
    (iterations / "1").symlink_to(outside, target_is_directory=True)
    iteration = iterations / "2"
    iteration.mkdir()
    (iteration / "status.json").symlink_to(outside / "status.json")
    (iteration / "result.json").symlink_to(outside / "result.json")
    assert research_history(tmp_path)["iterations"] == []
    (iteration / "status.json").unlink()
    write_record(tmp_path, "iterations/2/status.json", {"status": "running"})
    history = research_history(tmp_path)
    assert history["iterations"][0]["missing_result"]
    assert "PRIVATE" not in json.dumps(history)
    (tmp_path / "result.json").symlink_to(outside / "result.json")
    (iterations / "1").unlink()
    (iteration / "result.json").unlink()
    (iteration / "status.json").unlink()
    iteration.rmdir()
    iterations.rmdir()
    iterations.symlink_to(outside, target_is_directory=True)
    assert research_history(tmp_path)["iterations"] == []
