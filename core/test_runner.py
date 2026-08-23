"""Internal pytest-only structured validation kernel (CODING-06A1B)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
import time

from core import emergency_stop, process_manager


RESULT_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
MAX_REPORT_BYTES = 256 * 1024
MAX_FAILURE_DETAILS = 25
MAX_NODE_ID_CHARS = 4096
MAX_EXCERPT_CHARS = 4096
MAX_SELECTORS = 100
MAX_SELECTOR_CHARS = 4096
MAX_COUNT = 10_000_000
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_TIMEOUT_SECONDS = 3600.0
MAX_DIAGNOSTIC_CHARS = 4096
REPORT_TEMP_PREFIX = "lumina-pytest-report-"

_REPORT_KEYS = frozenset({
    "schema_version", "nonce", "complete", "pytest_exit_status", "counts",
    "relevant_failures", "failure_details_truncated",
})
_COUNT_KEYS = frozenset({
    "collected", "passed", "failed", "skipped", "errors", "xfailed", "xpassed",
})
_DETAIL_KEYS = frozenset({"node_id", "phase", "kind", "excerpt"})
_PHASES = frozenset({"collection", "setup", "call", "teardown"})
_KINDS = frozenset({"failure", "error"})

_structured_run_lock = threading.Lock()


@dataclass(frozen=True)
class TestRunResult:
    schema_version: int
    status: str
    check_kind: str
    exit_code: int | None
    duration_seconds: float
    counts: dict | None
    relevant_failures: tuple
    report_state: str
    failure_details_truncated: bool
    termination_reason: str | None = None
    diagnostic: str | None = None


class _ReportError(Exception):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state = state


def _validate_selectors(selectors) -> tuple[str, ...]:
    if isinstance(selectors, (str, bytes, bytearray)) or not isinstance(selectors, Sequence):
        raise ValueError("selectors must be a sequence of strings")
    if len(selectors) > MAX_SELECTORS:
        raise ValueError(f"at most {MAX_SELECTORS} selectors are allowed")
    normalized = []
    for index, selector in enumerate(selectors):
        if not isinstance(selector, str):
            raise ValueError(f"selectors[{index}] must be a string")
        if not selector:
            raise ValueError(f"selectors[{index}] must not be empty")
        if len(selector) > MAX_SELECTOR_CHARS:
            raise ValueError(
                f"selectors[{index}] exceeds {MAX_SELECTOR_CHARS} characters"
            )
        if "\0" in selector:
            raise ValueError(f"selectors[{index}] must not contain NUL")
        normalized.append(selector)
    return tuple(normalized)


def _validate_timeout(timeout) -> float:
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
            or timeout > MAX_TIMEOUT_SECONDS):
        raise ValueError(
            f"timeout must be a finite number greater than 0 and at most {MAX_TIMEOUT_SECONDS:g}"
        )
    return float(timeout)


def _validate_cwd(cwd: str) -> str:
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("cwd must be an absolute path string")
    if not os.path.isdir(cwd):
        raise ValueError("cwd must exist and be a directory")
    return cwd


def _load_report(report_path: Path, expected_nonce: str) -> dict:
    try:
        size = report_path.stat().st_size
    except FileNotFoundError as exc:
        raise _ReportError("missing", "structured pytest report is missing") from exc
    except OSError as exc:
        raise _ReportError("invalid", "structured pytest report cannot be inspected") from exc
    if size <= 0:
        raise _ReportError("malformed", "structured pytest report is empty")
    if size > MAX_REPORT_BYTES:
        raise _ReportError("oversized", "structured pytest report exceeds its size limit")

    try:
        with report_path.open("rb") as stream:
            open_size = os.fstat(stream.fileno()).st_size
            if open_size <= 0:
                raise _ReportError("malformed", "structured pytest report is empty")
            if open_size > MAX_REPORT_BYTES:
                raise _ReportError(
                    "oversized", "structured pytest report exceeds its size limit"
                )
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise _ReportError("oversized", "structured pytest report exceeds its size limit")
        text = raw.decode("utf-8", errors="strict")
        report = json.loads(text)
    except _ReportError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ReportError("malformed", "structured pytest report is not valid UTF-8 JSON") from exc

    if type(report) is not dict or frozenset(report) != _REPORT_KEYS:
        raise _ReportError("invalid", "structured pytest report has unsupported fields")
    if type(report["schema_version"]) is not int or report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise _ReportError("invalid", "structured pytest report schema is unsupported")
    if type(report["nonce"]) is not str or not secrets.compare_digest(
        report["nonce"], expected_nonce
    ):
        raise _ReportError("invalid", "structured pytest report nonce does not match")
    if report["complete"] is not True:
        raise _ReportError("invalid", "structured pytest report is incomplete")
    exit_status = report["pytest_exit_status"]
    if type(exit_status) is not int or not 0 <= exit_status <= 255:
        raise _ReportError("invalid", "structured pytest exit status is invalid")
    if type(report["failure_details_truncated"]) is not bool:
        raise _ReportError("invalid", "failure-details truncation flag is invalid")

    counts = report["counts"]
    if type(counts) is not dict or frozenset(counts) != _COUNT_KEYS:
        raise _ReportError("invalid", "structured pytest counters have unsupported fields")
    if any(type(value) is not int or not 0 <= value <= MAX_COUNT for value in counts.values()):
        raise _ReportError("invalid", "structured pytest counters are invalid")
    primary = sum(counts[key] for key in ("passed", "failed", "skipped", "xfailed", "xpassed"))
    if primary > counts["collected"]:
        raise _ReportError("invalid", "structured pytest counters are internally impossible")

    details = report["relevant_failures"]
    if type(details) is not list or len(details) > MAX_FAILURE_DETAILS:
        raise _ReportError("invalid", "structured pytest failure details are invalid")
    for detail in details:
        if type(detail) is not dict or frozenset(detail) != _DETAIL_KEYS:
            raise _ReportError("invalid", "structured pytest failure detail has unsupported fields")
        if (type(detail["node_id"]) is not str
                or len(detail["node_id"]) > MAX_NODE_ID_CHARS
                or type(detail["excerpt"]) is not str
                or len(detail["excerpt"]) > MAX_EXCERPT_CHARS
                or detail["phase"] not in _PHASES
                or detail["kind"] not in _KINDS):
            raise _ReportError("invalid", "structured pytest failure detail is invalid")

    failure_state = counts["failed"] + counts["errors"]
    failure_detail_count = sum(detail["kind"] == "failure" for detail in details)
    error_detail_count = sum(detail["kind"] == "error" for detail in details)
    if (failure_detail_count > counts["failed"] + counts["xpassed"]
            or error_detail_count > counts["errors"]):
        raise _ReportError("invalid", "failure details contradict reported counters")
    if exit_status == 0 and (failure_state or details):
        raise _ReportError("invalid", "pytest success contradicts failure/error counters")
    if exit_status == 1 and not (
        failure_state or (counts["xpassed"] and failure_detail_count)
    ):
        raise _ReportError("invalid", "pytest failure lacks a failure/error outcome")
    if exit_status == 5 and (counts["collected"] != 0 or primary or counts["errors"]):
        raise _ReportError("invalid", "no-tests exit contradicts reported outcomes")
    return report


def _diagnostic_from_snapshot(snapshot, reason: str) -> str:
    reason = reason[:1024]
    pieces = [reason]
    stream_budget = max(0, (MAX_DIAGNOSTIC_CHARS - len(reason) - 20) // 2)
    for name in ("stdout", "stderr"):
        stream = snapshot.get(name) if isinstance(snapshot, dict) else None
        text = stream.get("text") if isinstance(stream, dict) else None
        if text:
            pieces.append(f"{name}: {text[-stream_budget:]}")
    return "\n".join(pieces)[:MAX_DIAGNOSTIC_CHARS]


def _result(*, status, started, exit_code=None, counts=None, details=(),
            report_state="not_started", truncated=False, termination_reason=None,
            diagnostic=None):
    return TestRunResult(
        schema_version=RESULT_SCHEMA_VERSION,
        status=status,
        check_kind="pytest",
        exit_code=exit_code,
        duration_seconds=max(0.0, time.monotonic() - started),
        counts=counts,
        relevant_failures=tuple(details),
        report_state=report_state,
        failure_details_truncated=truncated,
        termination_reason=termination_reason,
        diagnostic=diagnostic,
    )


def _classify(snapshot: dict, report: dict | None, report_state: str,
              report_reason: str | None, started: float) -> TestRunResult:
    process_status = snapshot["status"]
    exit_code = snapshot["returncode"]
    termination_reason = snapshot.get("termination_reason")

    forced_status = {
        "timed_out": "timed_out",
        "cancelled": "cancelled",
        "emergency_killed": "interrupted",
        "shutdown_killed": "interrupted",
        "stopped": "interrupted",
    }.get(process_status)

    if report is not None and exit_code != report["pytest_exit_status"]:
        report = None
        report_state = "incompatible"
        report_reason = "parent-observed process exit contradicts the child report"

    counts = report["counts"] if report is not None else None
    details = report["relevant_failures"] if report is not None else ()
    truncated = report["failure_details_truncated"] if report is not None else False

    if forced_status is not None:
        return _result(
            status=forced_status, started=started, exit_code=exit_code,
            counts=counts, details=details, report_state=report_state,
            truncated=truncated, termination_reason=termination_reason,
            diagnostic=(
                _diagnostic_from_snapshot(snapshot, report_reason)
                if report_reason else None
            ),
        )

    if process_status != "exited":
        return _result(
            status="runner_error", started=started, exit_code=exit_code,
            report_state=report_state, termination_reason=termination_reason,
            diagnostic=_diagnostic_from_snapshot(
                snapshot, report_reason or f"unexpected managed process status: {process_status}"
            ),
        )

    if exit_code not in (0, 1, 2, 3, 4, 5):
        return _result(
            status="abnormal_exit", started=started, exit_code=exit_code,
            counts=counts, details=details, report_state=report_state,
            truncated=truncated, termination_reason=termination_reason,
            diagnostic=_diagnostic_from_snapshot(
                snapshot, report_reason or "pytest child exited abnormally"
            ),
        )
    if report is None:
        return _result(
            status="runner_error", started=started, exit_code=exit_code,
            report_state=report_state, termination_reason=termination_reason,
            diagnostic=_diagnostic_from_snapshot(
                snapshot, report_reason or "structured pytest report is unavailable"
            ),
        )

    if exit_code == 0:
        status = "passed"
    elif exit_code == 1:
        status = "failed"
    elif exit_code == 2:
        status = (
            "collection_error"
            if any(detail["phase"] == "collection" for detail in details)
            else "interrupted"
        )
    elif exit_code == 5:
        status = "no_tests_collected"
    else:
        status = "runner_error"

    diagnostic = None
    if status == "runner_error":
        diagnostic = _diagnostic_from_snapshot(
            snapshot, f"pytest exited with runner status {exit_code}"
        )
    return _result(
        status=status, started=started, exit_code=exit_code, counts=counts,
        details=details, report_state=report_state, truncated=truncated,
        termination_reason=termination_reason, diagnostic=diagnostic,
    )


class TestRunner:
    """Run one structured pytest child at a time without serializing A1A."""

    def run_pytest(self, *, cwd: str, selectors=(), timeout=DEFAULT_TIMEOUT_SECONDS,
                   cancel_event=None) -> TestRunResult:
        started = time.monotonic()
        cwd = _validate_cwd(cwd)
        selectors = _validate_selectors(selectors)
        timeout = _validate_timeout(timeout)
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            raise ValueError("cancel_event must provide is_set()")

        if not _structured_run_lock.acquire(blocking=False):
            return _result(status="runner_busy", started=started)

        process_id = None
        snapshot = None
        try:
            with tempfile.TemporaryDirectory(prefix=REPORT_TEMP_PREFIX) as temporary_dir:
                temporary_path = Path(temporary_dir)
                report_path = temporary_path / "pytest-report.json"
                nonce = secrets.token_hex(32)
                child_runner = str(Path(__file__).with_name("pytest_child_runner.py").resolve())
                argv = [
                    sys.executable, "-u", child_runner,
                    "--report", str(report_path), "--nonce", nonce,
                ]
                if selectors:
                    argv.extend(("--", *selectors))

                env = dict(os.environ)
                env.pop("PYTEST_ADDOPTS", None)
                env.pop("PYTEST_PLUGINS", None)
                env["GIT_TERMINAL_PROMPT"] = "0"
                env["GCM_INTERACTIVE"] = "Never"
                env["PYTHONPYCACHEPREFIX"] = str(temporary_path / "pycache")

                try:
                    process_id = process_manager.launch_argv(
                        argv, cwd=cwd, env=env,
                        provenance={"kind": "structured_pytest", "schema_version": 1},
                        visibility="internal",
                    )
                    snapshot = process_manager.wait_for_completion(
                        process_id, timeout=timeout, cancel_event=cancel_event,
                    )
                except process_manager.LaunchRefused as exc:
                    return _result(
                        status="interrupted" if emergency_stop.is_latched() else "runner_error",
                        started=started, report_state="not_started",
                        termination_reason=("emergency_killed" if emergency_stop.is_latched() else None),
                        diagnostic=str(exc)[:MAX_DIAGNOSTIC_CHARS],
                    )
                except process_manager.ProcessManagerError as exc:
                    return _result(
                        status="runner_error", started=started, report_state="not_started",
                        diagnostic=str(exc)[:MAX_DIAGNOSTIC_CHARS],
                    )

                try:
                    report = _load_report(report_path, nonce)
                    report_state = "complete"
                    report_reason = None
                except _ReportError as exc:
                    report = None
                    report_state = exc.state
                    report_reason = str(exc)
                return _classify(
                    snapshot, report, report_state, report_reason, started,
                )
        finally:
            if process_id is not None and snapshot is not None:
                process_manager.forget_terminal_job(process_id)
            _structured_run_lock.release()


_default_runner = TestRunner()


def run_pytest(*, cwd: str, selectors=(), timeout=DEFAULT_TIMEOUT_SECONDS,
               cancel_event=None) -> TestRunResult:
    return _default_runner.run_pytest(
        cwd=cwd, selectors=selectors, timeout=timeout, cancel_event=cancel_event,
    )
