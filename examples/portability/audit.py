"""One deterministic CPU audit; the Group host owns execution identity."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
import xml.etree.ElementTree as ET

TESTS = (
    "tests/test_environment.py",
    "tests/test_group_agents.py",
    "tests/test_exp_workflow.py",
    "tests/test_reference_workflow.py",
    "tests/test_publication.py",
)


def junit_counts(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    cases = list(root.iter("testcase"))
    if not suites or not cases:
        raise ValueError("JUnit must contain actual test cases")
    counts = {
        key: sum(int(suite.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    actual = {
        "tests": len(cases),
        "failures": sum(case.find("failure") is not None for case in cases),
        "errors": sum(case.find("error") is not None for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
    }
    if counts != actual:
        raise ValueError("JUnit summary and test cases disagree")
    expected = {name[:-3].replace("/", ".") for name in TESTS}
    if {case.get("classname") for case in cases} != expected:
        raise ValueError("JUnit does not match the exact frozen test selection")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path.cwd())
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    work = args.output.resolve()
    if work.is_relative_to(project):
        parser.error("Audit outputs must be outside the subject checkout")
    work.mkdir(parents=True, exist_ok=True)
    revision = subprocess.check_output(
        ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
    ).strip()
    import research_loop

    module = Path(research_loop.__file__).resolve()
    if Path(sys.prefix) != project / ".venv" or not module.is_relative_to(
        project / "src/research_loop"
    ):
        raise RuntimeError("Select this checkout's locked Python environment")
    junit = work / "junit.xml"
    log = work / "pytest.log"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--junitxml",
        str(junit),
        "--basetemp",
        str(work / "pytest-temp"),
        *TESTS,
    ]
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    for name in (
        "RESEARCH_NATIVE_TEST",
        "RESEARCH_MODEL_TEST",
        "RESEARCH_ENVIRONMENT_MODEL_TEST",
    ):
        environment.pop(name, None)
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    started = perf_counter()
    with log.open("w") as stream:
        completed = subprocess.run(
            command,
            cwd=project,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
    elapsed = perf_counter() - started
    if completed.returncode not in (0, 1):
        raise RuntimeError(f"Pytest did not finish normally: {completed.returncode}")
    metrics = {
        "source_revision": revision,
        "platform": platform.platform(),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "pytest_version": importlib.metadata.version("pytest"),
        "research_loop_module_path": str(module),
        "test_selection": list(TESTS),
        "pytest_exit_code": completed.returncode,
        "elapsed_seconds": elapsed,
        **junit_counts(junit),
        "junit_path": str(junit),
        "log_path": str(log),
    }
    (work / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (work / "report.md").write_text(
        "# Repository audit\n\n"
        f"Platform: {metrics['platform']}\n\nRevision: {revision}\n\n"
        f"Pytest exit: {completed.returncode}; tests: {metrics['tests']}; "
        f"failures: {metrics['failures']}; errors: {metrics['errors']}; "
        f"skipped: {metrics['skipped']}.\n\n"
        "This is execution evidence. Independent evaluation and Study publication "
        "are separate Group stages; this report does not assert either is complete.\n"
    )


if __name__ == "__main__":
    main()
