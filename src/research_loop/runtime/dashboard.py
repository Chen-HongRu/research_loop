"""Loopback-only, read-only monitoring of the generic SQLite runtime."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
from pathlib import Path
import socket
import sqlite3
from urllib.parse import urlsplit

from .status import status_snapshot
from .store import StateStoreError


_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research runtime status</title>
<style>
body{font:15px system-ui,sans-serif;background:#f4f6f8;color:#18232d;margin:0;padding:24px;max-width:1400px}
h1{margin-top:0}section{background:white;border:1px solid #d5dde4;border-radius:10px;padding:16px;margin:18px 0;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:10px;border-bottom:1px solid #e2e7eb;vertical-align:top}
th,td{white-space:nowrap}td:first-child{white-space:normal;overflow-wrap:anywhere;min-width:12rem;max-width:24rem}p{line-height:1.5}#error{color:#a52c32}small{color:#526471}
</style>
<h1>Research runtime</h1><p id="summary">Reading persisted status…</p><p id="error" role="status"></p>
<small>Read-only records, refreshed every 3 seconds. Owner-open is a recorded fact, not proof of liveness.
Elapsed time is the persisted ledger value. Terminal execution and retired callbacks do not prove physical release or final fees.</small>
<section><h2>Tasks</h2><div id="tasks"></div></section>
<section><h2>Pending or rejected requests</h2><div id="requests"></div></section>
<section><h2>Imported legacy records</h2><div id="legacy"></div></section>
<section><h2>Operations</h2><div id="operations"></div></section>
<section><h2>Agent callbacks</h2><div id="attempts"></div></section>
<script>
'use strict';
const text=value=>value===null||value===undefined?'—':typeof value==='object'?JSON.stringify(value):String(value);
function table(id,columns,rows){
 const host=document.getElementById(id);host.replaceChildren();
 if(!rows.length){host.textContent='No records.';return;}
 const grid=document.createElement('table'),head=document.createElement('tr');
 columns.forEach(([title])=>{const cell=document.createElement('th');cell.scope='col';cell.textContent=title;head.append(cell);});grid.append(head);
 rows.forEach(row=>{const line=document.createElement('tr');columns.forEach(([,field])=>{const cell=document.createElement('td');cell.textContent=text(typeof field==='function'?field(row):row[field]);line.append(cell);});grid.append(line);});host.append(grid);
}
async function refresh(){try{
 const response=await fetch('api/status',{cache:'no-store'});if(!response.ok)throw Error('Status temporarily unavailable');const data=await response.json();
 document.getElementById('summary').textContent=`Study ${data.study_id} · schema ${data.schema_version} · admission ${data.admission_mode} · STOP ${data.stop.requested?'requested':'not requested'} · recorded owner ${data.owner_open?'open':'closed'} · calls ${data.counters.calls}/${data.limits.max_calls??'unlimited'} · iterations ${data.counters.iterations}/${data.limits.max_iterations??'unlimited'} · persisted elapsed ${Number(data.counters.elapsed_seconds).toFixed(2)}s/${data.limits.max_seconds}s · publication ${data.publication_version} · reported ${data.reported_at}`;
 table('tasks',[['Task','id'],['Source','source'],['Status','status'],['Phase','phase'],['Ready','ready'],['Dependency blockers','blocked_dependencies'],['Execution','execution'],['Feedback sealed','feedback_sealed'],['Physical released','physical_released'],['Fees settled','fees_settled']],data.tasks);
 table('operations',[['Operation','id'],['Backend','backend'],['State','state'],['Outcome','terminal_outcome'],['Callback',row=>row.dispatch?.state],['Callback retired',row=>row.dispatch?.worker_retired],['Unresolved','unresolved'],['Physical released','physical_released'],['Resource holds','held_resources'],['Cost holds','held_costs'],['Actual costs','actual_costs'],['Fees final','costs_final'],['Collection','collection_state'],['Intake','intake'],['Feedback sealed','feedback_sealed']],data.operations);
 table('attempts',[['Attempt','id'],['Role','role'],['Status','status'],['Worker state',row=>row.dispatch?.state],['Callback retired',row=>row.dispatch?.worker_retired],['Applied',row=>row.dispatch?.settled],['Unresolved','unresolved']],data.attempts);
 table('requests',[['Request','request_id'],['Kind','kind'],['Status','status']],data.control_requests);
 table('legacy',[['Kind','kind'],['Original ID','legacy_id'],['Original status','native_status']],data.legacy_records);
 document.getElementById('error').textContent='';
 }catch(error){document.getElementById('error').textContent=error.message;}}
refresh();setInterval(refresh,3000);
</script></html>"""


def make_server(root, host="127.0.0.1", port=0, *, research=False):
    """Create a local server without starting its blocking service loop."""
    host = "127.0.0.1" if host == "localhost" else host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ValueError("Dashboard bind must be a loopback address") from error
    if not address.is_loopback:
        raise ValueError("Dashboard bind must be a loopback address")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid dashboard port")
    root = Path(root).resolve()

    pages = {"/": _HTML.encode()}
    projections = {"/api/status": status_snapshot}
    if research:
        from .overview import overview_snapshot

        pages["/ledger"] = pages["/"]
        pages["/"] = Path(__file__).with_name("overview.html").read_bytes()
        projections["/api/overview"] = overview_snapshot

    class Handler(BaseHTTPRequestHandler):
        def parse_request(self):
            if not super().parse_request():
                return False
            hosts = self.headers.get_all("Host", [])
            authority = hosts[0].strip().lower() if len(hosts) == 1 else ""
            if not re.fullmatch(
                r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", authority
            ):
                self.send_error(403, "Loopback Host required")
                return False
            if (
                authority.rsplit(":", 1)[-1].isdigit()
                and not 1 <= int(authority.rsplit(":", 1)[-1]) <= 65535
            ):
                self.send_error(403, "Loopback Host required")
                return False
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in pages:
                body, content_type = pages[path], "text/html; charset=utf-8"
            elif path in projections:
                try:
                    body = json.dumps(
                        projections[path](root), ensure_ascii=False, allow_nan=False
                    ).encode()
                except (OSError, ValueError, StateStoreError, sqlite3.Error):
                    self.send_error(503, "Status unavailable")
                    return
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405, "Read-only dashboard")

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        daemon_threads = True

    return Server((host, port), Handler)


def serve(root, host="127.0.0.1", port=0, *, research=False):
    """Serve until interrupted; always close the listening socket."""
    with make_server(root, host, port, research=research) as server:
        display_host = f"[{host}]" if ":" in host else host
        print(
            f"Runtime dashboard: http://{display_host}:{server.server_port}", flush=True
        )
        server.serve_forever()
