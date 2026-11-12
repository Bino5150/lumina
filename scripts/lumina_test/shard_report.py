"""Structured pytest observations adapted from SAFE-TEST-SHARDS-01."""
from builtins import open as _open_report
import json
from os import fsync as _sync_report

import pytest


class Reporter:
    def __init__(self, path, selection, rules):
        self.path = path
        self.selection = selection
        self.rules = rules
        self.refused = False

    def write(self, record):
        with _open_report(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            _sync_report(stream.fileno())

    def pytest_sessionstart(self, session):
        self.write({"event": "sessionstart"})

    def profile(self, item):
        node = item.nodeid
        if item.get_closest_marker("castle_walls_evidence"):
            return "evidence"
        file = node.split("::", 1)[0]
        if item.get_closest_marker("lumina_wire") or any(
            node == prefix or node.startswith(prefix + "::") or node.startswith(prefix + "[")
            for prefix in self.rules["wire"]
        ):
            return "wire"
        if item.get_closest_marker("lumina_qt") or file in self.rules["qt"]:
            return "qt"
        return "core"

    def pytest_collection_finish(self, session):
        items = session.items
        observed = [(item.nodeid, self.profile(item)) for item in items]
        if self.selection is not None:
            by_id = {item.nodeid: item for item in items}
            expected = self.selection["inventory"]
            actual = dict(observed)
            self.refused = (len(actual) != len(observed) or actual != expected
                            or session.testsfailed > 0)
            session.items = [] if self.refused else [
                by_id[node] for node in self.selection["execute"]
            ]
            session.testscollected = len(session.items)
        self.write({"event": "collection", "observed": observed,
                    "nodeids": [item.nodeid for item in session.items]})

    def pytest_runtestloop(self, session):
        if self.refused:
            pytest.exit("maintained inventory changed; use lumina-test list", returncode=4)

    def pytest_runtest_logreport(self, report):
        self.write({
            "event": "testreport",
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "when": report.when,
            "wasxfail": bool(getattr(report, "wasxfail", False)),
            "longrepr": str(report.longrepr)[:2000] if report.longrepr else "",
        })

    def pytest_collectreport(self, report):
        if report.outcome in ("failed", "skipped"):
            self.write({"event": "collectreport", "nodeid": report.nodeid,
                        "outcome": report.outcome, "longrepr": str(report.longrepr)[:2000]})

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        try:
            self.write({"event": "sessionfinish", "exitstatus": int(exitstatus)})
        except Exception:
            session.exitstatus = pytest.ExitCode.INTERNAL_ERROR
            raise
