"""TEST-RUNTIME-01: managed run_tests must execute the requested checkout.

The managed pytest kernel (core/test_runner.py) launches its child with
``sys.executable core/pytest_child_runner.py`` from the MANAGING checkout.
Two proven contamination routes existed:

1. Script-directory insertion: CPython puts the child runner's directory
   (the managing checkout's ``core/``) at the child's ``sys.path[0]``,
   ahead of the standard library -- so a stdlib-colliding module in the
   managing tree (``core/secrets.py`` is the real historical case)
   shadowed true stdlib modules for every import in the child.
2. Inherited PYTHONPATH: the child environment was ``dict(os.environ)``;
   any parent-runtime PYTHONPATH entry landed ahead of stdlib and could
   smuggle another checkout's modules into the target's test run.

The repair: the child runner removes its own directory from sys.path
before importing anything else; the kernel drops inherited PYTHONPATH
entirely (clean-slate contract -- see the sanitizer's docstring for why
selective filtering is untrustworthy). Blast radius of the sanitization
is exactly the one variable.

These tests prove isolation with disposable sentinel checkouts (A and B)
that carry same-named modules with distinguishable content, a
stdlib-colliding ``core/secrets.py`` sentinel, and a probe test that
records import provenance into an evidence JSON file. No Lumina-pathname
assumptions: the sentinels live in tmp_path, proving isolation derives
from the requested root, not from directory names.
"""

from __future__ import annotations

import json
import os
import secrets as stdlib_secrets
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from core import emergency_stop, process_manager, test_runner


TEST_ROOT = Path(__file__).resolve().parent.parent
CHILD_RUNNER = TEST_ROOT / "core" / "pytest_child_runner.py"


@pytest.fixture(autouse=True)
def _clean_process_state():
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()
    yield
    process_manager._reset_for_tests()
    emergency_stop._reset_for_tests()


# ---------------------------------------------------------------------------
# Sentinel checkout construction
# ---------------------------------------------------------------------------

_SECRETS_SENTINEL = '''"""TEST-RUNTIME-01 sentinel: stdlib-colliding module."""
import importlib.util, os, sysconfig
_real = os.path.join(sysconfig.get_paths()["stdlib"], "secrets.py")
_spec = importlib.util.spec_from_file_location("_tr01_real_secrets", _real)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("__")})
CORE_SENTINEL = "@SENTINEL@"
'''

_PROBE_SOURCE = '''"""TEST-RUNTIME-01 probe: records import provenance, asserts isolation."""
import importlib, json, os, subprocess, sys, sysconfig


def _record_and_assert():
    evidence_path = os.environ["PROBE_EVIDENCE"]
    evidence = {
        "cwd": os.getcwd(),
        "interpreter": sys.executable,
        "sys_path": list(sys.path),
        "pythonpath_env": os.environ.get("PYTHONPATH", ""),
        "qt_platform": os.environ.get("QT_QPA_PLATFORM", ""),
    }
    evidence["module_source"] = importlib.import_module("module").SOURCE
    try:
        import secrets as _secrets
        evidence["secrets_file"] = getattr(_secrets, "__file__", None)
        evidence["secrets_sentinel"] = getattr(_secrets, "CORE_SENTINEL", None)
    except Exception as exc:
        evidence["secrets_file"] = "IMPORT-ERROR: %r" % (exc,)
        evidence["secrets_sentinel"] = None
    try:
        evidence["shadow_sentinel"] = importlib.import_module("shadow_module").SENTINEL
    except Exception:
        evidence["shadow_sentinel"] = None
    try:
        evidence["unrelated_helper"] = importlib.import_module("unrelated_helper").UNRELATED
    except Exception:
        evidence["unrelated_helper"] = None
    if os.environ.get("PROBE_RUN_GIT"):
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True)
        evidence["git_version_rc"] = proc.returncode
    with open(evidence_path, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream)

    if os.environ.get("PROBE_MODE") == "fail":
        raise AssertionError("deliberate probe failure for exit-propagation proof")

    expected_source = os.environ.get("PROBE_EXPECT_SOURCE", "A")
    assert evidence["module_source"] == expected_source, evidence["module_source"]
    expected_interpreter = os.environ.get("PROBE_EXPECT_INTERPRETER")
    if expected_interpreter:
        assert evidence["interpreter"] == expected_interpreter, evidence["interpreter"]
    stdlib_dir = os.path.realpath(sysconfig.get_paths()["stdlib"])
    secrets_file = evidence["secrets_file"]
    assert secrets_file and "IMPORT-ERROR" not in str(secrets_file), secrets_file
    forbidden_root = os.environ.get("PROBE_FORBIDDEN_ROOT")
    if forbidden_root:
        assert not str(secrets_file).startswith(forbidden_root), secrets_file
    assert os.path.realpath(os.path.dirname(str(secrets_file))) == stdlib_dir, secrets_file
    assert evidence["secrets_sentinel"] is None, evidence["secrets_sentinel"]
    assert evidence["shadow_sentinel"] is None, evidence["shadow_sentinel"]
    if os.environ.get("PROBE_RUN_GIT"):
        assert evidence["git_version_rc"] == 0, evidence["git_version_rc"]


def test_probe():
    _record_and_assert()
'''


def _make_sentinel_checkout(root: Path, source: str, sentinel: str):
    (root / "core").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "module.py").write_text(f"SOURCE = {source!r}\n", encoding="utf-8")
    (root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "core" / "secrets.py").write_text(
        _SECRETS_SENTINEL.replace("@SENTINEL@", sentinel), encoding="utf-8")
    (root / "core" / "shadow_module.py").write_text(
        f'SENTINEL = {sentinel!r}\n', encoding="utf-8")
    (root / "tests" / "conftest.py").write_text(
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n",
        encoding="utf-8")
    (root / "tests" / "test_probe.py").write_text(_PROBE_SOURCE, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\ntestpaths = tests\n", encoding="utf-8")


@pytest.fixture
def checkout_a(tmp_path):
    root = tmp_path / "checkout_a"
    _make_sentinel_checkout(root, "A", "A-core")
    return root


@pytest.fixture
def checkout_b(tmp_path):
    root = tmp_path / "checkout_b"
    _make_sentinel_checkout(root, "B", "B-core")
    return root


def _run_kernel(target: Path, evidence_path: Path, *, selectors=None,
                pythonpath=None, expect_source="A"):
    if pythonpath is not None:
        os.environ["PYTHONPATH"] = pythonpath
    os.environ["PROBE_EVIDENCE"] = str(evidence_path)
    os.environ["PROBE_EXPECT_SOURCE"] = expect_source
    os.environ["PROBE_EXPECT_INTERPRETER"] = sys.executable
    os.environ["PROBE_FORBIDDEN_ROOT"] = str(TEST_ROOT) + os.sep
    try:
        result = test_runner.run_pytest(
            cwd=str(target), selectors=list(selectors or []), timeout=120,
        )
    finally:
        os.environ.pop("PYTHONPATH", None)
        os.environ.pop("PROBE_EVIDENCE", None)
        os.environ.pop("PROBE_EXPECT_SOURCE", None)
        os.environ.pop("PROBE_EXPECT_INTERPRETER", None)
        os.environ.pop("PROBE_FORBIDDEN_ROOT", None)
    return result, evidence_path


def _load_evidence(evidence_path: Path) -> dict:
    assert evidence_path.exists(), "probe never ran; no evidence recorded"
    return json.loads(evidence_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A/C/D/E/F/G/I: kernel-level isolation against a foreign-checkout PYTHONPATH
# ---------------------------------------------------------------------------

def test_kernel_run_isolated_from_foreign_and_unrelated_pythonpath(
        checkout_a, checkout_b, tmp_path):
    """Foreign-checkout and unrelated PYTHONPATH entries must not reach the
    child: no stdlib shadowing, no foreign module import, clean-slate
    inherited import surface, target-local imports intact, correct cwd and
    interpreter, and no managing-checkout directory anywhere on the child's
    sys.path."""
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "unrelated_helper.py").write_text(
        'UNRELATED = "preserved"\n', encoding="utf-8")
    pythonpath = os.pathsep.join([str(unrelated), str(checkout_b / "core")])

    result, evidence_path = _run_kernel(
        checkout_a, tmp_path / "evidence.json",
        selectors=["tests/test_probe.py"], pythonpath=pythonpath,
    )

    assert result.status == "passed", result.diagnostic
    assert result.exit_code == 0
    evidence = _load_evidence(evidence_path)
    stdlib_dir = os.path.realpath(sysconfig.get_paths()["stdlib"])
    assert os.path.realpath(os.path.dirname(evidence["secrets_file"])) == stdlib_dir
    assert evidence["secrets_sentinel"] is None
    assert evidence["shadow_sentinel"] is None
    assert evidence["unrelated_helper"] is None
    assert evidence["module_source"] == "A"
    assert evidence["cwd"] == str(checkout_a)
    assert evidence["interpreter"] == sys.executable
    assert evidence["pythonpath_env"] == ""
    assert all(
        not entry.startswith(str(TEST_ROOT) + os.sep)
        for entry in evidence["sys_path"] if entry
    )


def test_kernel_child_sys_path_excludes_managing_core(checkout_a, tmp_path):
    """Route-1 proof at kernel level: the managing checkout's core/ directory
    must not appear anywhere on the child's sys.path."""
    result, evidence_path = _run_kernel(
        checkout_a, tmp_path / "evidence.json", selectors=["tests/test_probe.py"],
    )
    assert result.status == "passed", result.diagnostic
    evidence = _load_evidence(evidence_path)
    managing_core = str(TEST_ROOT / "core")
    assert all(
        os.path.abspath(entry) != managing_core
        for entry in evidence["sys_path"] if entry
    )


# ---------------------------------------------------------------------------
# B (reverse direction): a foreign tree managing the run cannot shadow stdlib
# ---------------------------------------------------------------------------

def test_foreign_managing_tree_child_runner_cannot_shadow_stdlib(
        checkout_a, checkout_b, tmp_path):
    """Simulates the real historical shape with roles proven symmetric: the
    foreign checkout B acts as the managing tree (its core/ contains the
    same repaired child runner). Running B's child runner against target A
    must leave B's sentinel modules unable to shadow stdlib."""
    (checkout_b / "core" / "pytest_child_runner.py").write_text(
        CHILD_RUNNER.read_text(encoding="utf-8"), encoding="utf-8")
    report_path = tmp_path / "report.json"
    nonce = stdlib_secrets.token_hex(32)
    evidence_path = tmp_path / "evidence.json"
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PROBE_EVIDENCE", "PROBE_EXPECT_SOURCE",
                       "PROBE_EXPECT_INTERPRETER", "PROBE_FORBIDDEN_ROOT")
    }
    env["PROBE_EVIDENCE"] = str(evidence_path)
    env["PROBE_EXPECT_SOURCE"] = "A"
    env["PROBE_EXPECT_INTERPRETER"] = sys.executable
    env["PROBE_FORBIDDEN_ROOT"] = str(checkout_b) + os.sep
    proc = subprocess.run(
        [sys.executable, "-u", str(checkout_b / "core" / "pytest_child_runner.py"),
         "--report", str(report_path), "--nonce", nonce, "--",
         "tests/test_probe.py"],
        cwd=str(checkout_a), env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["pytest_exit_status"] == 0
    assert report["counts"]["passed"] == 1
    evidence = _load_evidence(evidence_path)
    stdlib_dir = os.path.realpath(sysconfig.get_paths()["stdlib"])
    assert os.path.realpath(os.path.dirname(evidence["secrets_file"])) == stdlib_dir
    assert evidence["secrets_sentinel"] is None
    assert all(
        os.path.abspath(entry) != str(checkout_b / "core")
        for entry in evidence["sys_path"] if entry
    )


# ---------------------------------------------------------------------------
# H: native exit propagation -- passing and failing children
# ---------------------------------------------------------------------------

def test_kernel_reports_failing_probe_with_truthful_exit(checkout_a, tmp_path):
    result, evidence_path = _run_kernel(
        checkout_a, tmp_path / "evidence.json", selectors=["tests/test_probe.py"],
    )
    assert result.status == "passed"
    os.environ["PROBE_MODE"] = "fail"
    os.environ["PROBE_EVIDENCE"] = str(tmp_path / "evidence_fail.json")
    os.environ["PROBE_EXPECT_SOURCE"] = "A"
    try:
        failing = test_runner.run_pytest(
            cwd=str(checkout_a), selectors=["tests/test_probe.py"], timeout=120,
        )
    finally:
        for key in ("PROBE_MODE", "PROBE_EVIDENCE", "PROBE_EXPECT_SOURCE"):
            os.environ.pop(key, None)
    assert failing.status == "failed"
    assert failing.exit_code == 1
    assert failing.counts["failed"] == 1


# ---------------------------------------------------------------------------
# I: no-selector invocation honors the target's own testpaths contract
# ---------------------------------------------------------------------------

def test_kernel_no_selector_run_uses_target_testpaths(checkout_a, tmp_path):
    result, evidence_path = _run_kernel(checkout_a, tmp_path / "evidence.json")
    assert result.status == "passed", result.diagnostic
    assert _load_evidence(evidence_path)["module_source"] == "A"


# ---------------------------------------------------------------------------
# M4 damage-case control: sanitization blast radius is exactly PYTHONPATH
# ---------------------------------------------------------------------------

def test_kernel_child_environment_preserves_path_and_other_variables(
        checkout_a, tmp_path):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["PROBE_RUN_GIT"] = "1"
    try:
        result, evidence_path = _run_kernel(
            checkout_a, tmp_path / "evidence.json",
            selectors=["tests/test_probe.py"],
        )
    finally:
        os.environ.pop("PROBE_RUN_GIT", None)
    assert result.status == "passed", result.diagnostic
    evidence = _load_evidence(evidence_path)
    assert evidence["git_version_rc"] == 0
    assert evidence["qt_platform"] == "offscreen"


# ---------------------------------------------------------------------------
# J: managed-worktree identity -- the worktree's own source wins
# ---------------------------------------------------------------------------

def test_kernel_run_inside_worktree_uses_worktree_source(checkout_a, tmp_path):
    def _git(*args):
        subprocess.run(["git", "-C", str(checkout_a), *args], check=True,
                       capture_output=True,
                       env={**os.environ,
                            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    (checkout_a / "module.py").write_text('SOURCE = "A"\n', encoding="utf-8")
    _git("init", "-q")
    _git("add", ".")
    _git("commit", "-q", "-m", "init")
    worktree = tmp_path / "worktree_w"
    _git("worktree", "add", "-q", str(worktree), "-b", "wt")
    (worktree / "module.py").write_text('SOURCE = "A-worktree"\n', encoding="utf-8")

    result, evidence_path = _run_kernel(
        worktree, tmp_path / "evidence.json",
        selectors=["tests/test_probe.py"], expect_source="A-worktree",
    )
    assert result.status == "passed", result.diagnostic
    assert _load_evidence(evidence_path)["module_source"] == "A-worktree"


# ---------------------------------------------------------------------------
# Sanitizer unit contract
# ---------------------------------------------------------------------------

def test_sanitize_strips_pythonpath_and_nothing_else():
    env = {"PYTHONPATH": f"/a{os.pathsep}/b", "PATH": "/usr/bin",
           "QT_QPA_PLATFORM": "offscreen", "HOME": "/home/someone"}
    test_runner._sanitize_inherited_pythonpath(env)
    assert "PYTHONPATH" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert env["HOME"] == "/home/someone"

    env = {"PATH": "/usr/bin"}
    test_runner._sanitize_inherited_pythonpath(env)
    assert env == {"PATH": "/usr/bin"}
