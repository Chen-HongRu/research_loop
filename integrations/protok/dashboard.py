"""Read-only local dashboard. No model calls, remote commands or research mutations."""

import argparse
import json
import sys
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from integrations.protok.backend import ProTokBackend


def read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def prose(path):
    try:
        return path.read_text()[:250_000]
    except OSError:
        return "暂无记录"


def response(folder, phase):
    attempts = sorted((folder / phase).glob("attempt-*/response.json"))
    return read(attempts[-1], {}) if attempts else {}


def snapshot(root, *, snapshot_only=False):
    study = root / "study"
    state = read(study / "state.json", {})
    best = read(root / "control/active-best.json", {})
    if snapshot_only:
        backend = read(root / "control/backend-snapshot.json")
        if not backend:
            raise ValueError("Missing exported backend snapshot")
    else:
        owner = read(root / "control/ownership.json", {})
        backend = ProTokBackend(Path(owner["project"]), root / "control").inspect()
    records = {r["fingerprint"]: r for r in backend["records"]}
    nodes = []
    for folder in sorted((study / "iterations").glob("[0-9]*")):
        meta = read(folder / "iteration.json", {})
        if not meta:
            continue
        identity = meta["id"]
        job = read(root / "control/jobs" / identity / "job.json", {})
        receipt = read(root / "control/jobs" / identity / "result.json", {})
        record = records.get(job.get("fingerprint"), {})
        review = response(folder, "review")
        published = (folder / "publication.json").exists()
        eligible = published and any(
            c.get("valid")
            and c.get("verified")
            and c.get("candidate_id") == receipt.get("fingerprint")
            for c in review.get("candidate_reviews", [])
        )
        active = int(identity) == state.get("iteration")
        live = state.get("status") if active else "reviewed"
        plan = prose(folder / "plan.md")
        nodes.append(
            {
                "id": identity,
                "route": meta.get("route_title", "路线选择中"),
                "route_id": meta.get("route_id"),
                "base": meta.get("base_experiment"),
                "references": meta.get("references", []),
                "status": live
                if active
                else ("reviewed" if published else meta.get("status")),
                "phase": state.get("phase") if active else "done",
                "published": published,
                "eligible": eligible,
                "value": receipt.get("value") if eligible else None,
                "provisional_value": record.get("summary", {}).get("value")
                if not eligible
                else None,
                "imported": receipt.get("provenance")
                == "historical-import-no-allocation",
                "attempts": record.get("attempts", []),
                "job_status": job.get("status"),
                "title": plan.split("\n")[0][:150],
                "created_at": meta.get("created_at"),
            }
        )
    return {
        "observed_at": time.time(),
        "snapshot_only": snapshot_only,
        "backend_observed_at": backend.get("observed_at"),
        "state": {
            k: state.get(k)
            for k in (
                "iteration",
                "phase",
                "status",
                "call_count",
                "error",
                "active_attempt",
                "stop_reason",
            )
        },
        "deadline": read(root / "control/continuation.json", {}).get("deadline"),
        "stop": (root / "control/STOP").exists(),
        "runner": read(root / "control/runner.json", {}),
        "best": {"id": best.get("experiment_id"), "metrics": best.get("metrics", {})},
        "gpu_seconds": backend["used_gpu_seconds"],
        "scored_records": backend["scored_records"],
        "outstanding": backend["outstanding"],
        "nodes": nodes,
        "routes": read(study / "routes/index.json", {}),
    }


def details(root, identity):
    folder = root / "study/iterations" / identity
    selection = read(folder / "selection.json", {})
    return {
        "plan": prose(folder / "plan.md"),
        "execute": prose(folder / "execute.md"),
        "review": prose(folder / "review.md"),
        "selection": json.dumps(selection, ensure_ascii=False, indent=2)
        if selection
        else "选择过程尚未发布",
        "route": prose(folder / "route.after.md"),
    }


def serve(root, port, *, snapshot_only=False):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == "/":
                    body = Path(__file__).with_suffix(".html").read_bytes()
                    mime = "text/html; charset=utf-8"
                elif path == "/api/snapshot":
                    body = json.dumps(
                        snapshot(root, snapshot_only=snapshot_only), ensure_ascii=False
                    ).encode()
                    mime = "application/json; charset=utf-8"
                elif re.fullmatch(r"/api/node/[0-9]{4,}", path):
                    body = json.dumps(
                        details(root, path.rsplit("/", 1)[1]), ensure_ascii=False
                    ).encode()
                    mime = "application/json; charset=utf-8"
                elif path == "/api/research":
                    body = json.dumps(
                        {"text": prose(root / "study/research.md")}, ensure_ascii=False
                    ).encode()
                    mime = "application/json; charset=utf-8"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError, KeyError) as error:
                self.send_error(503, type(error).__name__)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Research dashboard: http://127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot-only", action="store_true")
    args = parser.parse_args()
    serve(args.root.resolve(), args.port, snapshot_only=args.snapshot_only)
