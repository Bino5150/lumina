"""Start pytest only after the kernel namespace proof succeeds."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, "/work/scripts/lumina_test")
from shard_report import Reporter

config = json.loads(Path("/controls/pytest.json").read_text())
if config["require_qt"]:
    from PySide6.QtWidgets import QApplication

    # Validate that Qt starts in the required profile before collecting any tests.
    app = QApplication([])
    if app.platformName() != "offscreen":
        raise RuntimeError("the required offscreen Qt platform did not start")

args = ["-c", "pytest.ini", "-o", "addopts=", "--strict-markers", "-q", "-ra",
        "-p", "no:cacheprovider", "--basetemp", "/tmp/t", *config["targets"]]
if config["collect"]:
    args.append("--collect-only")
raise SystemExit(pytest.main(args, plugins=[
    Reporter("/out/report.jsonl", config["selection"], config["rules"]),
]))
