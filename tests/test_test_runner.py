"""Parent-side A1B structured pytest kernel and managed lifecycle tests."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from core import emergency_stop, process_manager, test_runner


@pytest.fixture(autouse=True)
def _clean_process_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


def _write(path, source):
    path.write_text(source, encoding="utf-8")


def _valid_report(nonce="expected"):
    return {
        "schema_version": 1,
        "nonce": nonce,
        "complete": True,
        "pytest_exit_status": 0,
        "counts": {
            "collected": 1, "passed": 1, "failed": 0, "skipped": 0,
            "errors": 0, "xfailed": 0, "xpassed": 0,
        },
        "relevant_failures": [],
        "failure_details_truncated": False,
    }


def _snapshot(*, status="exited", returncode=0, stdout="", stderr=""):
    return {
        "status": status,
        "returncode": returncode,
        "termination_reason": None if status == "exited" else status,
        "stdout": {"text": stdout},
        "stderr": {"text": stderr},
    }


def _write_report(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("selectors", [
    None,
    "test_file.py",
    [""],
    [7],
    ["bad\0selector"],
    ["x" * (test_runner.MAX_SELECTOR_CHARS + 1)],
    ["x"] * (test_runner.MAX_SELECTORS + 1),
])
def test_selector_validation_fails_before_launch(monkeypatch, tmp_path, selectors):
    monkeypatch.setattr(
        process_manager, "launch_argv",
        lambda *a, **k: pytest.fail("invalid selector reached process launch"),
    )
    with pytest.raises(ValueError):
        test_runner.run_pytest(cwd=str(tmp_path), selectors=selectors)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), 3601, "30"])
def test_timeout_validation_fails_before_launch(monkeypatch, tmp_path, timeout):
    monkeypatch.setattr(
        process_manager, "launch_argv",
        lambda *a, **k: pytest.fail("invalid timeout reached process launch"),
    )
    with pytest.raises(ValueError):
        test_runner.run_pytest(cwd=str(tmp_path), timeout=timeout)


def test_parent_rejects_missing_empty_malformed_and_oversized_reports(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(test_runner._ReportError, match="missing") as exc_info:
        test_runner._load_report(missing, "expected")
    assert exc_info.value.state == "missing"

    cases = ((b"", "malformed"), (b"\xff", "malformed"), (b"{", "malformed"),
             (b"x" * (test_runner.MAX_REPORT_BYTES + 1), "oversized"))
    for index, (raw, state) in enumerate(cases):
        path = tmp_path / f"case-{index}.json"
        path.write_bytes(raw)
        with pytest.raises(test_runner._ReportError) as caught:
            test_runner._load_report(path, "expected")
        assert caught.value.state == state


def test_parent_rejects_structurally_invalid_reports(tmp_path):
    mutations = []
    for mutate in (
        lambda value: value.update(schema_version=2),
        lambda value: value.update(nonce="wrong"),
        lambda value: value.update(complete=False),
        lambda value: value.update(extra="field"),
        lambda value: value["counts"].update(passed=True),
        lambda value: value["counts"].update(failed=-1),
        lambda value: value["counts"].update(passed=2),
        lambda value: value.update(relevant_failures=[{
            "node_id": "n", "phase": "unknown", "kind": "failure", "excerpt": "e",
        }]),
    ):
        payload = _valid_report()
        mutate(payload)
        mutations.append(payload)

    for index, payload in enumerate(mutations):
        path = tmp_path / f"invalid-{index}.json"
        _write_report(path, payload)
        with pytest.raises(test_runner._ReportError) as caught:
            test_runner._load_report(path, "expected")
        assert caught.value.state == "invalid"


def test_parent_rejects_report_exit_and_counter_contradictions(tmp_path):
    success_with_failure = _valid_report()
    success_with_failure["counts"].update(passed=0, failed=1)
    failure_claiming_success = _valid_report()
    failure_claiming_success["pytest_exit_status"] = 1
    no_tests_with_test = _valid_report()
    no_tests_with_test["pytest_exit_status"] = 5

    for index, payload in enumerate(
        (success_with_failure, failure_claiming_success, no_tests_with_test)
    ):
        path = tmp_path / f"contradiction-{index}.json"
        _write_report(path, payload)
        with pytest.raises(test_runner._ReportError) as caught:
            test_runner._load_report(path, "expected")
        assert caught.value.state == "invalid"


def test_parent_observed_exit_wins_over_complete_success_report():
    result = test_runner._classify(
        _snapshot(returncode=7), _valid_report(), "complete", None, time.monotonic(),
    )
    assert result.status == "abnormal_exit"
    assert result.report_state == "incompatible"
    assert result.counts is None


def test_timeout_and_cancel_statuses_cannot_be_rewritten_by_success_report():
    for process_status in ("timed_out", "cancelled"):
        snapshot = _snapshot(status=process_status, returncode=0)
        result = test_runner._classify(
            snapshot, _valid_report(), "complete", None, time.monotonic(),
        )
        assert result.status == process_status


def test_exit_zero_with_report_removed_at_shutdown_is_not_passed(tmp_path):
    _write(tmp_path / "test_remove_report.py", """
import atexit
from pathlib import Path
import sys

def remove_report():
    report = Path(sys.argv[sys.argv.index('--report') + 1])
    report.unlink()

atexit.register(remove_report)

def test_pass():
    assert True
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 0
    assert result.status == "runner_error"
    assert result.report_state == "missing"


def test_exit_zero_with_report_corrupted_at_shutdown_is_not_passed(tmp_path):
    _write(tmp_path / "test_corrupt_report.py", """
import atexit
from pathlib import Path
import sys

def corrupt_report():
    report = Path(sys.argv[sys.argv.index('--report') + 1])
    report.write_text('{', encoding='utf-8')

atexit.register(corrupt_report)

def test_pass():
    assert True
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 0
    assert result.status == "runner_error"
    assert result.report_state == "malformed"


@pytest.mark.parametrize("mutation,expected_state", [
    ("partial", "invalid"),
    ("wrong_nonce", "invalid"),
    ("wrong_schema", "invalid"),
    ("oversized", "oversized"),
])
def test_shutdown_report_tampering_fails_closed(tmp_path, mutation, expected_state):
    _write(tmp_path / "test_tamper_report.py", f"""
import atexit
import json
from pathlib import Path
import sys

def tamper_report():
    report = Path(sys.argv[sys.argv.index('--report') + 1])
    mutation = {mutation!r}
    if mutation == 'partial':
        report.write_text('{{"schema_version": 1}}', encoding='utf-8')
    elif mutation == 'oversized':
        report.write_bytes(b'x' * ({test_runner.MAX_REPORT_BYTES} + 1))
    else:
        payload = json.loads(report.read_text(encoding='utf-8'))
        if mutation == 'wrong_nonce':
            payload['nonce'] = 'attacker-controlled'
        else:
            payload['schema_version'] = 999
        report.write_text(json.dumps(payload), encoding='utf-8')

atexit.register(tamper_report)

def test_pass():
    assert True
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 0
    assert result.status == "runner_error"
    assert result.report_state == expected_state


def test_success_looking_terminal_text_then_os_exit_is_abnormal(tmp_path):
    _write(tmp_path / "test_crash.py", """
import os

def test_crash():
    print('999 passed in 0.01s', flush=True)
    os._exit(7)
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 7
    assert result.status == "abnormal_exit"
    assert result.report_state == "missing"
    assert result.counts is None


def test_complete_success_report_then_atexit_crash_is_abnormal(tmp_path):
    _write(tmp_path / "test_late_crash.py", """
import atexit
import os

atexit.register(lambda: os._exit(7))

def test_pass():
    assert True
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 7
    assert result.status == "abnormal_exit"
    assert result.report_state == "incompatible"
    assert result.counts is None


def test_failure_report_then_atexit_success_override_is_not_passed(tmp_path):
    _write(tmp_path / "test_false_success.py", """
import atexit
import os

atexit.register(lambda: os._exit(0))

def test_fail():
    assert False
""")
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    assert result.exit_code == 0
    assert result.status == "runner_error"
    assert result.report_state == "incompatible"
    assert result.counts is None


def test_launch_is_fixed_absolute_argv_and_selectors_follow_double_dash(monkeypatch, tmp_path):
    weird = "test weird λ 'quotes' ;&$().py"
    _write(tmp_path / weird, "def test_ok(): assert True\n")
    captured = {}
    real_launch = process_manager.launch_argv

    def capture_launch(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return real_launch(argv, **kwargs)

    monkeypatch.setattr(process_manager, "launch_argv", capture_launch)
    monkeypatch.setattr(
        process_manager, "launch",
        lambda *a, **k: pytest.fail("structured pytest used shell launch"),
    )
    result = test_runner.run_pytest(cwd=str(tmp_path), selectors=[weird], timeout=30)

    assert result.status == "passed"
    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert os.path.isabs(argv[2])
    assert Path(argv[2]).name == "pytest_child_runner.py"
    assert "--basetemp" not in argv
    assert "no:cacheprovider" not in argv
    assert argv[-2:] == ["--", weird]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert captured["kwargs"]["visibility"] == "internal"


def test_leading_hyphen_filename_is_literal_selector(tmp_path):
    filename = "-test_leading.py"
    _write(tmp_path / filename, "def test_ok(): assert True\n")
    result = test_runner.run_pytest(cwd=str(tmp_path), selectors=[filename], timeout=30)
    assert result.status == "passed"
    assert result.counts["passed"] == 1


@pytest.mark.parametrize("selector,artifact_name", [
    ("--junitxml=escaped.xml", "escaped.xml"),
    ("--basetemp=escaped-temp", "escaped-temp"),
    ("--rootdir=escaped-root", "escaped-root"),
    ("$(touch escaped-shell)", "escaped-shell"),
    (";touch escaped-redirection", "escaped-redirection"),
    (">escaped-output", "escaped-output"),
])
def test_option_and_shell_like_selectors_remain_literal_data(
    tmp_path, selector, artifact_name
):
    result = test_runner.run_pytest(cwd=str(tmp_path), selectors=[selector], timeout=30)
    assert result.status != "passed"
    assert not (tmp_path / artifact_name).exists()


def test_plugin_selector_cannot_load_a_plugin(tmp_path):
    marker = tmp_path / "plugin-loaded"
    _write(tmp_path / "evil_plugin.py", f"""
from pathlib import Path
Path({str(marker)!r}).write_text('loaded', encoding='utf-8')
""")
    result = test_runner.run_pytest(
        cwd=str(tmp_path), selectors=["-p", "evil_plugin"], timeout=30,
    )
    assert result.status != "passed"
    assert not marker.exists()


def test_child_environment_removes_pytest_overrides_and_sets_noninteractive_git(
    monkeypatch, tmp_path
):
    plugin_marker = tmp_path / "plugin-loaded"
    execution_marker = tmp_path / "test-executed"
    _write(tmp_path / "evil_plugin.py", f"""
from pathlib import Path
Path({str(plugin_marker)!r}).write_text('loaded', encoding='utf-8')
""")
    _write(tmp_path / "test_environment.py", f"""
import os
from pathlib import Path

def test_environment_contract():
    assert 'PYTEST_ADDOPTS' not in os.environ
    assert 'PYTEST_PLUGINS' not in os.environ
    assert os.environ['GIT_TERMINAL_PROMPT'] == '0'
    assert os.environ['GCM_INTERACTIVE'] == 'Never'
    assert os.environ['PYTHONPYCACHEPREFIX']
    Path({str(execution_marker)!r}).write_text('executed', encoding='utf-8')
""")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    monkeypatch.setenv("PYTEST_PLUGINS", "evil_plugin")

    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "passed"
    assert execution_marker.exists()
    assert not plugin_marker.exists()


def test_second_structured_run_is_busy_without_second_launch(monkeypatch, tmp_path):
    started_file = tmp_path / "started"
    _write(tmp_path / "test_slow.py", f"""
from pathlib import Path
import time

def test_slow():
    Path({str(started_file)!r}).write_text('started', encoding='utf-8')
    time.sleep(30)
""")
    cancel_event = threading.Event()
    launched = []
    real_launch = process_manager.launch_argv

    def capture_launch(*args, **kwargs):
        process_id = real_launch(*args, **kwargs)
        launched.append(process_id)
        return process_id

    monkeypatch.setattr(process_manager, "launch_argv", capture_launch)
    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault(
        "result", test_runner.run_pytest(
            cwd=str(tmp_path), timeout=30, cancel_event=cancel_event,
        )
    ))
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started_file.exists()
        second = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
        assert second.status == "runner_busy"
        assert second.exit_code is None
        assert len(launched) == 1
    finally:
        cancel_event.set()
        thread.join(timeout=10)
    assert holder["result"].status == "cancelled"


def test_structured_lock_does_not_serialize_process_manager(monkeypatch, tmp_path):
    started_file = tmp_path / "started"
    _write(tmp_path / "test_slow.py", f"""
from pathlib import Path
import time

def test_slow():
    Path({str(started_file)!r}).write_text('started', encoding='utf-8')
    time.sleep(30)
""")
    cancel_event = threading.Event()
    thread = threading.Thread(target=lambda: test_runner.run_pytest(
        cwd=str(tmp_path), timeout=30, cancel_event=cancel_event,
    ))
    thread.start()
    other_id = None
    try:
        deadline = time.monotonic() + 10
        while not started_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started_file.exists()
        other_id = process_manager.launch_argv(
            [sys.executable, "-c", "print('independent')"], cwd=str(tmp_path),
        )
        other = process_manager.wait_for_completion(other_id, timeout=10)
        assert other["status"] == "exited"
        assert other["returncode"] == 0
    finally:
        if other_id is not None:
            process_manager.forget_terminal_job(other_id)
        cancel_event.set()
        thread.join(timeout=10)


def test_internal_job_hidden_and_forgotten_after_harvest(monkeypatch, tmp_path):
    _write(tmp_path / "test_ok.py", "def test_ok(): assert True\n")
    process_ids = []
    forgotten = []
    real_launch = process_manager.launch_argv
    real_forget = process_manager.forget_terminal_job

    def capture_launch(*args, **kwargs):
        process_id = real_launch(*args, **kwargs)
        process_ids.append(process_id)
        assert process_id not in process_manager.list_job_ids()
        assert process_manager.get_job_snapshot(process_id) is None
        return process_id

    def capture_forget(process_id):
        forgotten.append(process_id)
        return real_forget(process_id)

    monkeypatch.setattr(process_manager, "launch_argv", capture_launch)
    monkeypatch.setattr(process_manager, "forget_terminal_job", capture_forget)
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "passed"
    assert forgotten == process_ids
    assert process_manager._get_internal_job_snapshot(process_ids[0]) is None


def test_managed_streams_are_drained_but_not_returned_as_result_fields(monkeypatch, tmp_path):
    _write(tmp_path / "conftest.py", """
import sys

def pytest_sessionfinish(session, exitstatus):
    sys.__stdout__.write('O' * 70000)
    sys.__stdout__.flush()
    sys.__stderr__.write('E' * 70000)
    sys.__stderr__.flush()
""")
    _write(tmp_path / "test_ok.py", "def test_ok(): assert True\n")
    harvested = {}
    real_forget = process_manager.forget_terminal_job

    def capture_forget(process_id):
        harvested.update(process_manager._get_internal_job_snapshot(process_id))
        return real_forget(process_id)

    monkeypatch.setattr(process_manager, "forget_terminal_job", capture_forget)
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)

    assert result.status == "passed"
    assert harvested["stdout"]["end_offset"] >= 70000
    assert harvested["stderr"]["end_offset"] >= 70000
    assert not hasattr(result, "stdout")
    assert not hasattr(result, "stderr")


def _write_descendant_suite(tmp_path):
    heartbeat = tmp_path / "descendant-heartbeat"
    started = tmp_path / "test-started"
    child_source = (
        "from pathlib import Path; import sys,time; p=Path(sys.argv[1]); i=0; "
        "\nwhile True:\n i += 1; p.write_text(str(i), encoding='utf-8'); time.sleep(0.02)"
    )
    _write(tmp_path / "test_descendant.py", f"""
from pathlib import Path
import subprocess
import sys
import time

def test_descendant():
    subprocess.Popen([sys.executable, '-c', {child_source!r}, {str(heartbeat)!r}])
    Path({str(started)!r}).write_text('started', encoding='utf-8')
    time.sleep(30)
""")
    return heartbeat, started


def _wait_for_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()


def _assert_heartbeat_stopped(path):
    _wait_for_file(path)
    before = path.read_text(encoding="utf-8")
    time.sleep(0.25)
    assert path.read_text(encoding="utf-8") == before


def test_timeout_kills_pytest_descendant_tree(tmp_path):
    heartbeat, _ = _write_descendant_suite(tmp_path)
    # Pytest/plugin startup itself is intentionally part of the timeout.  Give
    # the fixture enough time to prove its descendant is live before expiry.
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=5)
    assert result.status == "timed_out"
    _assert_heartbeat_stopped(heartbeat)


def test_cancellation_kills_pytest_descendant_tree(tmp_path):
    heartbeat, started = _write_descendant_suite(tmp_path)
    cancel_event = threading.Event()
    timer = threading.Thread(target=lambda: (_wait_for_file(heartbeat), cancel_event.set()))
    timer.start()
    result = test_runner.run_pytest(
        cwd=str(tmp_path), timeout=30, cancel_event=cancel_event,
    )
    timer.join(timeout=5)
    assert result.status == "cancelled"
    _assert_heartbeat_stopped(heartbeat)


def test_emergency_kills_pytest_descendant_tree_and_is_distinguishable(tmp_path):
    heartbeat, started = _write_descendant_suite(tmp_path)
    holder = {}
    thread = threading.Thread(target=lambda: holder.setdefault(
        "result", test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    ))
    thread.start()
    try:
        _wait_for_file(heartbeat)
        emergency_stop.latch(source="test", reason="A1B emergency probe")
        assert process_manager.emergency_kill_all() == 1
        thread.join(timeout=10)
        assert not thread.is_alive()
        result = holder["result"]
        assert result.status == "interrupted"
        assert result.termination_reason == "emergency_killed"
        _assert_heartbeat_stopped(heartbeat)
    finally:
        if emergency_stop.is_latched() and emergency_stop.can_rearm():
            emergency_stop.rearm_local()


def test_latched_emergency_refuses_new_structured_child(monkeypatch, tmp_path):
    launches = []
    real_launch = process_manager.launch_argv

    def capture_launch(*args, **kwargs):
        launches.append(1)
        return real_launch(*args, **kwargs)

    monkeypatch.setattr(process_manager, "launch_argv", capture_launch)
    emergency_stop.latch(source="test", reason="A1B admission probe")
    try:
        result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
        assert result.status == "interrupted"
        assert result.termination_reason == "emergency_killed"
        # The attempted managed admission is visible at the API boundary, but
        # A1A rejects it before process creation, lease, slot, or registry use.
        assert launches == [1]
        assert process_manager._registry == {}
        assert process_manager._live_reserved == 0
    finally:
        emergency_stop.rearm_local()


def test_report_temp_directory_is_removed_after_harvest(tmp_path):
    _write(tmp_path / "test_ok.py", "def test_ok(): assert True\n")
    temp_root = Path(test_runner.tempfile.gettempdir())
    before = set(temp_root.glob(test_runner.REPORT_TEMP_PREFIX + "*"))
    result = test_runner.run_pytest(cwd=str(tmp_path), timeout=30)
    after = set(temp_root.glob(test_runner.REPORT_TEMP_PREFIX + "*"))
    assert result.status == "passed"
    assert after == before
    assert not list(tmp_path.glob("*pytest-report*"))
