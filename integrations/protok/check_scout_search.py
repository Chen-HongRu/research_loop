"""One bounded, read-only smoke check of the deployed Codex search capability."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from research_loop.harness import CodexHarness, HarnessConfig  # noqa: E402
from research_loop.scout import SCOUT_SCHEMA, retrieval_events, validate_result  # noqa: E402


def main():
    root, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=True)
    config = HarnessConfig(
        **json.loads((root / "study/study.json").read_text())["harness"]
    )
    result = CodexHarness(config, web_search=True).run(
        prompt=(
            "Perform a read-only web-search smoke test. Search and open the original "
            "paper AlphaFold Meets Flow Matching for Generating Protein Ensembles "
            "and its authors' implementation. Return the supplied research scout "
            "schema with a concise factual summary, correctly labelled sources, "
            "search_status and search_notes. Return candidates=[]. Do not use shell "
            "commands, read local files, write files, or run experiments. If search "
            "is unavailable, report that honestly. This is a capability check only."
        ),
        schema=SCOUT_SCHEMA,
        workspace=output,
        run_dir=output,
        writable=False,
        timeout_seconds=300,
    )
    validate_result(result)
    events = retrieval_events(output / "stdout.jsonl")
    record = {
        "search_status": result["search_status"],
        "web_tool_events": events,
        "source_count": len(result["sources"]),
    }
    (output / "check.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record), flush=True)
    return 0 if events > 0 and result["search_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
