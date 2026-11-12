"""The real audit must preserve negative results and exact test selection."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def audit():
    source = Path(__file__).parents[1] / "examples/portability/audit.py"
    spec = importlib.util.spec_from_file_location("portability_audit", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def xml(audit, path, *, tests=5, unrelated=False):
    cases = []
    for index, name in enumerate(audit.TESTS):
        module = name[:-3].replace("/", ".")
        if unrelated and index == 0:
            module = "tests.unrelated"
        result = ("<failure/>", "<error/>", "<skipped/>", "", "")[index]
        cases.append(f'<testcase classname="{module}" name="check">{result}</testcase>')
    path.write_text(
        f'<testsuites><testsuite tests="{tests}" failures="1" errors="1" skipped="1">'
        + "".join(cases)
        + "</testsuite></testsuites>"
    )


def test_junit_keeps_failures_errors_and_skips(audit, tmp_path):
    path = tmp_path / "junit.xml"
    xml(audit, path)
    assert audit.junit_counts(path) == dict(tests=5, failures=1, errors=1, skipped=1)


def test_junit_rejects_inconsistent_summary(audit, tmp_path):
    path = tmp_path / "junit.xml"
    xml(audit, path, tests=6)
    with pytest.raises(ValueError, match="disagree"):
        audit.junit_counts(path)


def test_junit_rejects_different_test_selection(audit, tmp_path):
    path = tmp_path / "junit.xml"
    xml(audit, path, unrelated=True)
    with pytest.raises(ValueError, match="exact frozen test selection"):
        audit.junit_counts(path)
