"""Multi-study HTTP routing against synthetic public records."""

import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from research_loop.dashboard.server import Studies, handler_for


def create(root, name, status):
    path = root / name
    path.mkdir()
    (path / "status.json").write_text(json.dumps({"status": status, "iteration": 1}))
    (path / "goal.md").write_text(name)
    (path / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "resource.tool",
                "tool": "gpu_status",
                "success": True,
                "arguments": {"study": name},
                "output": {"study": name},
            }
        )
        + "\n"
    )
    return path


def test_catalog_dynamic_discovery_and_cached_reader_isolation(tmp_path):
    a = create(tmp_path, "a", "running")
    studies = Studies([a, a], tmp_path)
    assert len(studies.catalog()["studies"]) == 1
    aid = studies.catalog()["default"]
    assert studies.select(aid) is studies.select(aid)
    b = create(tmp_path, "b", "completed")
    (tmp_path / "linked").symlink_to(b, target_is_directory=True)
    assert [s["name"] for s in studies.catalog()["studies"]] == ["a", "b"]
    assert studies.select("../b") is None
    assert studies.select("") is None
    assert studies.select(aid).root == a
    b.rename(tmp_path / "archived")
    assert studies.select(studies.identifier(b)) is None


def test_http_scopes_state_metrics_tools_and_unknown_ids(tmp_path):
    a = create(tmp_path, "a", "running")
    b = create(tmp_path, "b", "completed")
    for path in (a, b):
        (path / "roles.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "threads": {
                        "same-child": {
                            "role": "critic",
                            "task": f"{path.name}-review",
                            "task_id": "same-task",
                            "status": "completed",
                        }
                    },
                }
            )
        )
        metrics = path / "metrics/current"
        metrics.mkdir(parents=True)
        (metrics / "latest.json").write_text(
            json.dumps({"phase": path.name, "llm": {}})
        )
        iteration = path / "iterations/000001"
        iteration.mkdir(parents=True)
        (iteration / "result.json").write_text(
            json.dumps({"summary": f"{path.name} result"})
        )
    studies = Studies(directory=tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(studies, {"localhost"}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def get(route):
        from urllib.request import Request

        with urlopen(
            Request(
                f"http://127.0.0.1:{httpd.server_port}{route}",
                headers={"Host": "localhost"},
            )
        ) as response:
            return json.load(response)

    try:
        assert len(get("/api/studies")["studies"]) == 2
        for path in (a, b):
            sid = studies.identifier(path)
            state = get(f"/api/state?study={sid}")
            assert state["goal"] == path.name
            assert state["agents"][0]["task_name"] == f"{path.name}-review"
            assert state["agents"][0]["role_source"] == "thread-record"
            assert (
                state["history"]["iterations"][0]["result"]["summary"]
                == f"{path.name} result"
            )
            assert get(f"/api/metrics?study={sid}")["phase"] == path.name
            tool = state["tools"][0]
            assert (
                get(f"/api/tool?study={sid}&id={tool['id']}")["output"]["study"]
                == path.name
            )
        assert get("/api/state")["goal"] == "a"
        for endpoint in ("state", "metrics", "tool"):
            with pytest.raises(HTTPError) as error:
                get(f"/api/{endpoint}?study=../../elsewhere")
            assert error.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
