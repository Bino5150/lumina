"""Behavioral probes for the child-side structured pytest reporter."""

import json

from core import emergency_stop, process_manager, pytest_child_runner, test_runner
from core.test_runner import MAX_FAILURE_DETAILS, run_pytest

import pytest


@pytest.fixture(autouse=True)
def _clean_process_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


def _write(path, source):
    path.write_text(source, encoding="utf-8")


def test_parent_and_child_report_contract_limits_match():
    assert pytest_child_runner.REPORT_SCHEMA_VERSION == test_runner.REPORT_SCHEMA_VERSION
    assert pytest_child_runner.MAX_REPORT_BYTES == test_runner.MAX_REPORT_BYTES
    assert pytest_child_runner.MAX_FAILURE_DETAILS == test_runner.MAX_FAILURE_DETAILS
    assert pytest_child_runner.MAX_NODE_ID_CHARS == test_runner.MAX_NODE_ID_CHARS
    assert pytest_child_runner.MAX_EXCERPT_CHARS == test_runner.MAX_EXCERPT_CHARS


def test_child_enforces_encoded_artifact_limit_without_losing_counts(tmp_path):
    reporter = pytest_child_runner.StructuredPytestReporter(
        tmp_path / "report.json", "nonce",
    )
    reporter.collected = 30
    reporter.counts["failed"] = 30
    reporter.relevant_failures = [
        {
            "node_id": "\\" * pytest_child_runner.MAX_NODE_ID_CHARS,
            "phase": "call",
            "kind": "failure",
            "excerpt": "\\" * pytest_child_runner.MAX_EXCERPT_CHARS,
        }
        for _ in range(pytest_child_runner.MAX_FAILURE_DETAILS)
    ]

    encoded = reporter._payload_bytes(1)
    payload = json.loads(encoded)

    assert len(encoded) <= pytest_child_runner.MAX_REPORT_BYTES
    assert payload["counts"]["failed"] == 30
    assert payload["failure_details_truncated"] is True
    assert len(payload["relevant_failures"]) < pytest_child_runner.MAX_FAILURE_DETAILS


def test_reporter_observes_pytest_9_outcomes_by_hook_semantics(tmp_path):
    _write(tmp_path / "test_outcomes.py", """
import pytest

def test_pass():
    assert True

def test_fail():
    assert False, "ordinary assertion"

@pytest.mark.skip(reason="skip probe")
def test_skip():
    pass

@pytest.mark.xfail(reason="xfail probe")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="xpass probe")
def test_xpass():
    assert True

@pytest.mark.xfail(reason="strict xpass probe", strict=True)
def test_strict_xpass():
    assert True

@pytest.fixture
def setup_boom():
    raise RuntimeError("setup probe")

def test_setup_error(setup_boom):
    pass

@pytest.fixture
def teardown_boom():
    yield
    raise RuntimeError("teardown probe")

def test_teardown_error(teardown_boom):
    assert True
""")

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.report_state == "complete"
    assert result.counts == {
        "collected": 8,
        "passed": 2,
        "failed": 1,
        "skipped": 1,
        "errors": 2,
        "xfailed": 1,
        "xpassed": 2,
    }
    assert {(item["phase"], item["kind"]) for item in result.relevant_failures} == {
        ("call", "failure"), ("setup", "error"), ("teardown", "error"),
    }


def test_reporter_distinguishes_collection_error_from_interruption(tmp_path):
    _write(tmp_path / "test_collection.py", "import module_that_does_not_exist_a1b\n")

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "collection_error"
    assert result.exit_code == 2
    assert result.counts["collected"] == 0
    assert result.counts["errors"] == 1
    assert result.relevant_failures[0]["phase"] == "collection"


def test_reporter_distinguishes_no_tests_collected(tmp_path):
    _write(tmp_path / "ordinary_module.py", "VALUE = 1\n")

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "no_tests_collected"
    assert result.exit_code == 5
    assert result.counts == {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "xfailed": 0,
        "xpassed": 0,
    }


def test_normal_project_cache_fixture_and_basetemp_semantics_are_preserved(tmp_path):
    _write(tmp_path / "test_project_semantics.py", """
def test_project_semantics(cache, request):
    assert request.config.option.basetemp is None
    cache.set('a1b/probe', {'value': 7})
    assert cache.get('a1b/probe', None) == {'value': 7}
""")

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "passed"
    assert result.counts["passed"] == 1


def test_failure_detail_cap_does_not_cap_exact_failure_count(tmp_path):
    tests = "\n".join(
        f"def test_failure_{index}():\n    assert False, 'failure {index}'\n"
        for index in range(MAX_FAILURE_DETAILS + 7)
    )
    _write(tmp_path / "test_many_failures.py", tests)

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "failed"
    assert result.counts["failed"] == MAX_FAILURE_DETAILS + 7
    assert len(result.relevant_failures) == MAX_FAILURE_DETAILS
    assert result.failure_details_truncated is True


def test_reporter_bounds_repository_controlled_node_ids_and_excerpts(tmp_path):
    long_id = "node-" + ("x" * 6000)
    _write(tmp_path / "test_long_failure.py", f"""
import pytest

@pytest.mark.parametrize("value", [1], ids=[{long_id!r}])
def test_long(value):
    assert False, {('message-' + ('y' * 10000))!r}
""")

    result = run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "failed"
    detail = result.relevant_failures[0]
    assert len(detail["node_id"]) <= 4096
    assert len(detail["excerpt"]) <= 4096
