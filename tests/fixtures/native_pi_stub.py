"""Offline Pi CLI fixture; configuration lives beside the copied executable."""

import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time

root = Path(__file__).resolve().parent
config = json.loads(Path(__file__).with_suffix(".json").read_text())
arguments = sys.argv[1:]
with (root / "arguments.jsonl").open("a") as stream:
    stream.write(json.dumps(arguments) + "\n")
assert "--no-context-files" in arguments
assert "submit_result" in arguments[arguments.index("--system-prompt") + 1]
assert arguments[arguments.index("--append-system-prompt") + 1] == ""
assert "--no-mcp" in arguments and "--no-extensions" in arguments
assert arguments.count("-e") == 1
extension = Path(arguments[arguments.index("-e") + 1])
assert (
    extension.is_absolute()
    and extension.name == "submit_result.ts"
    and extension.is_file()
)
binding = json.loads(os.environ["RL_PI_BINDING"])
assert (
    isinstance(binding["schema"], dict) and binding["deadline_ms"] > time.time() * 1000
)
if "--list-models" in arguments:
    assert "--offline" in arguments and "--no-tools" in arguments
    print("openai-codex " + config["catalog"])
    sys.exit(0)
assert "--no-tools" not in arguments
assert arguments[arguments.index("--tools") + 1] == "submit_result"
assert arguments[arguments.index("--provider") + 1] == "openai-codex"
assert arguments[arguments.index("--model") + 1] == "synthetic-model"
assert arguments[arguments.index("--thinking") + 1] == "medium"
prompt = sys.stdin.read()
schema = json.loads(
    prompt.split("The complete response must satisfy this JSON schema:\n", 1)[1]
)
assert binding["schema"] == schema
assert not Path("response-schema.json").exists()
package = json.loads(
    prompt.split("ContextPackage:\n", 1)[1].split("\nResponse contract:\n", 1)[0]
)
if package.get("encoding") == "shared-json-subtrees-v1":
    view = package
    package = view["package"]
    for replacement in view["replacements"]:
        parent = package
        for key in replacement["path"][:-1]:
            parent = parent[key]
        parent[replacement["path"][-1]] = json.loads(
            json.dumps(view["shared_values"][replacement["value_id"]])
        )
role = package["manifest"]["role"]
assert package["context"]["goal"] == "Synthetic native roles"
# Synthetic fixtures may retain their own supplied prompt for exact-delivery
# assertions; the production transport must retain no prompt/tool transcript.
with (root / "fixture-prompts.jsonl").open("a") as stream:
    stream.write(json.dumps({"role": role, "prompt": prompt}) + "\n")


def bridge(action, **fields):
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(binding["socket"])
        client.sendall((json.dumps({"action": action, **fields}) + "\n").encode())
        response = json.loads(client.makefile("rb").readline())
    assert response["ok"], response
    return response["result"]


assert bridge("provider_request") == {"ordinal": 1}
if config["hanging"]:
    if config["spawn_child"]:
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; time.sleep(3); Path("
                + repr(str(root / "late-child"))
                + ").write_text('late')",
            ]
        )
    (root / "cli-ready").write_text(str(os.getpgid(0)))
    time.sleep(30)
outputs = config["outputs"]
refs = {"scout": ["source:used"], "review": ["receipt:m"]}.get(role, [])
answer = (
    config["response"]
    if config["response"] is not None
    else {"output": outputs[role], "evidence_refs": refs}
)
bridge("submit", arguments=answer)
tool_id = "synthetic-submission"
message = {
    "role": "assistant",
    "stopReason": "toolUse",
    "provider": "openai-codex",
    "model": config["model"],
    "thinkingLevel": config["thinking"],
    "content": (config["extra_blocks"] or [])
    + [
        {
            "type": "toolCall",
            "id": tool_id,
            "name": "submit_result",
            "arguments": answer,
        }
    ],
}
start = {
    "type": "tool_execution_start",
    "toolCallId": tool_id,
    "toolName": "submit_result",
    "args": answer,
}
result = {"content": [], "details": answer, "terminate": True}
end = {
    "type": "tool_execution_end",
    "toolCallId": tool_id,
    "toolName": "submit_result",
    "result": result,
    "isError": False,
}
tool_result = {
    "role": "toolResult",
    "toolCallId": tool_id,
    "toolName": "submit_result",
    "content": [],
    "details": answer,
    "isError": False,
}
events = [
    {"type": "message_end", "message": message},
    start,
    end,
    {"type": "message_end", "message": tool_result},
]
case = config["event_case"]
if case == "plain_text":
    message["stopReason"] = "stop"
    message["content"] = [{"type": "text", "text": json.dumps(answer)}]
    events = events[:1]
elif case == "missing_end":
    events = events[:2]
elif case == "missing_start":
    events.remove(start)
elif case == "mismatched_start_args":
    start["args"] = {"output": {}, "evidence_refs": []}
elif case == "missing_tool_result":
    events.pop()
elif case == "mismatched_result_details":
    tool_result["details"] = {"output": {}, "evidence_refs": []}
elif case == "failed_tool":
    end["isError"] = True
elif case == "missing_terminate":
    del result["terminate"]
elif case == "wrong_tool":
    message["content"][-1]["name"] = "bash"
elif case == "mismatched_end_id":
    end["toolCallId"] = "forged-id"
elif case == "mismatched_result_id":
    tool_result["toolCallId"] = "forged-id"
elif case == "forged_end":
    events = events[1:]
elif case == "mismatched_details":
    end["result"] = {
        "content": [],
        "details": {"output": {}, "evidence_refs": []},
        "terminate": True,
    }
elif case == "duplicate_tool":
    message["content"].append(
        {
            "type": "toolCall",
            "id": "extra-id",
            "name": "submit_result",
            "arguments": answer,
        }
    )
elif case == "extra_assistant":
    events.append(
        {
            "type": "message_end",
            "message": {
                **message,
                "stopReason": "stop",
                "content": [{"type": "text", "text": "Additional response"}],
            },
        }
    )
elif case == "extra_execution":
    events.append({**end, "toolCallId": "extra-id", "toolName": "bash"})
events.append({"type": "agent_settled", "aborted": case == "aborted"})
for event in events:
    print(json.dumps(event, ensure_ascii=False))
