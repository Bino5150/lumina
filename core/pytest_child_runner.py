"""Small child-side pytest reporter for Lumina's structured test kernel.

This file is executed by absolute path in a managed child interpreter.  It is
not a model tool and it deliberately contains no project/default-cwd logic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest


REPORT_SCHEMA_VERSION = 1
MAX_REPORT_BYTES = 256 * 1024
MAX_FAILURE_DETAILS = 25
MAX_NODE_ID_CHARS = 4096
MAX_EXCERPT_CHARS = 4096


def _bounded(value, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    return text[:limit]


def _failure_excerpt(report) -> str:
    """Extract bounded, useful longrepr data without captured stream sections."""
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return ""
    if isinstance(longrepr, str):
        return _bounded(longrepr, MAX_EXCERPT_CHARS)

    pieces = []
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None:
        path = _bounded(getattr(crash, "path", ""), MAX_EXCERPT_CHARS)
        lineno = getattr(crash, "lineno", None)
        message = _bounded(getattr(crash, "message", ""), MAX_EXCERPT_CHARS)
        location = f"{path}:{lineno}" if path and lineno is not None else path
        pieces.extend(part for part in (location, message) if part)

    traceback = getattr(longrepr, "reprtraceback", None)
    entries = getattr(traceback, "reprentries", ()) if traceback is not None else ()
    if entries:
        lines = getattr(entries[-1], "lines", ())
        for line in lines[-8:]:
            remaining = MAX_EXCERPT_CHARS - sum(len(part) + 1 for part in pieces)
            if remaining <= 0:
                break
            pieces.append(_bounded(line, remaining))

    if not pieces:
        pieces.append(_bounded(longrepr, MAX_EXCERPT_CHARS))
    return "\n".join(pieces)[:MAX_EXCERPT_CHARS]


class StructuredPytestReporter:
    """Collect exact hook outcomes and atomically finalize one JSON report."""

    def __init__(self, report_path: Path, nonce: str):
        self.report_path = report_path
        self.nonce = nonce
        self.collected = 0
        self.counts = {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "errors": 0,
            "xfailed": 0,
            "xpassed": 0,
        }
        self.relevant_failures = []
        self.failure_details_truncated = False

    def _record_detail(self, report, *, phase: str, kind: str):
        if len(self.relevant_failures) >= MAX_FAILURE_DETAILS:
            self.failure_details_truncated = True
            return
        self.relevant_failures.append({
            "node_id": _bounded(getattr(report, "nodeid", ""), MAX_NODE_ID_CHARS),
            "phase": phase,
            "kind": kind,
            "excerpt": _failure_excerpt(report),
        })

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    def pytest_collectreport(self, report):
        if report.failed:
            self.counts["errors"] += 1
            self._record_detail(report, phase="collection", kind="error")

    def pytest_runtest_logreport(self, report):
        phase = report.when
        if report.failed:
            if phase == "call":
                # pytest 9.1.1 reports strict XPASS as a failed call with an
                # xfail keyword and a structured longrepr marker.
                is_strict_xpass = (
                    "xfail" in getattr(report, "keywords", {})
                    and isinstance(getattr(report, "longrepr", None), str)
                    and report.longrepr.startswith("[XPASS(strict)]")
                )
                counter = "xpassed" if is_strict_xpass else "failed"
                self.counts[counter] += 1
                self._record_detail(report, phase="call", kind="failure")
            else:
                self.counts["errors"] += 1
                self._record_detail(report, phase=phase, kind="error")
            return

        if report.skipped:
            counter = "xfailed" if hasattr(report, "wasxfail") else "skipped"
            self.counts[counter] += 1
            return

        if report.passed and phase == "call":
            counter = "xpassed" if hasattr(report, "wasxfail") else "passed"
            self.counts[counter] += 1

    def _payload_bytes(self, exit_status: int) -> bytes:
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "nonce": self.nonce,
            "complete": True,
            "pytest_exit_status": exit_status,
            "counts": {"collected": self.collected, **self.counts},
            "relevant_failures": list(self.relevant_failures),
            "failure_details_truncated": self.failure_details_truncated,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")

        # JSON escaping can expand repository-controlled text.  Drop only
        # diagnostic detail until the artifact itself satisfies the hard cap;
        # counters always remain exact.
        while len(encoded) > MAX_REPORT_BYTES and payload["relevant_failures"]:
            payload["relevant_failures"].pop()
            payload["failure_details_truncated"] = True
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
            ).encode("utf-8")
        if len(encoded) > MAX_REPORT_BYTES:
            raise RuntimeError("structured pytest report exceeds its hard size limit")
        return encoded

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        encoded = self._payload_bytes(int(exitstatus))
        temporary = self.report_path.with_name(self.report_path.name + ".tmp")
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.report_path)
        try:
            directory_fd = os.open(str(self.report_path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)


def _parse_child_argv(argv):
    if len(argv) < 4 or argv[0] != "--report" or argv[2] != "--nonce":
        raise ValueError("invalid structured pytest child invocation")
    report_path = Path(argv[1])
    nonce = argv[3]
    remainder = argv[4:]
    if remainder:
        if remainder[0] != "--":
            raise ValueError("pytest selectors must follow --")
        selectors = remainder[1:]
        if not selectors:
            raise ValueError("-- requires at least one pytest selector")
    else:
        selectors = []
    if not report_path.is_absolute() or not nonce:
        raise ValueError("report path must be absolute and nonce must be nonempty")
    return report_path, nonce, selectors


def main(argv=None) -> int:
    try:
        report_path, nonce, selectors = _parse_child_argv(
            list(sys.argv[1:] if argv is None else argv)
        )
    except ValueError as exc:
        print(f"structured pytest runner: {exc}", file=sys.stderr)
        return 4

    reporter = StructuredPytestReporter(report_path, nonce)
    pytest_args = ["--", *selectors] if selectors else []
    return int(pytest.main(pytest_args, plugins=[reporter]))


if __name__ == "__main__":
    raise SystemExit(main())
