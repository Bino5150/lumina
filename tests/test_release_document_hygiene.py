"""Release-tree guard for internal campaign documentation."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOT_MARKDOWN = {"README.md", "TELEGRAM_SETUP.md"}
EVIDENCE_PROBE = "project-evidence/campaign-reports/_document-hygiene-probe.md"
PRIVATE_REPORT_PREFIXES = ("reports/", "project-evidence/")


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_only_approved_root_markdown_is_tracked() -> None:
    result = _run_git("ls-files", "--cached", "--", "*.md")
    assert result.returncode == 0, result.stderr

    tracked_root_markdown = {
        path for path in result.stdout.splitlines() if "/" not in path
    }
    unexpected = tracked_root_markdown - ALLOWED_ROOT_MARKDOWN
    missing = ALLOWED_ROOT_MARKDOWN - tracked_root_markdown

    assert not unexpected, (
        "Internal campaign evidence must live under "
        "project-evidence/campaign-reports; it is not release source. "
        f"Unapproved root Markdown: {sorted(unexpected)}"
    )
    assert not missing, f"Required public documentation is not tracked: {sorted(missing)}"


def test_project_evidence_directory_is_ignored() -> None:
    result = _run_git("check-ignore", "--quiet", "--no-index", EVIDENCE_PROBE)
    assert result.returncode == 0, (
        "project-evidence/ must remain ignored so internal campaign evidence "
        "cannot enter release commits"
    )


def test_private_report_directories_are_not_tracked() -> None:
    result = _run_git("ls-files", "--cached")
    assert result.returncode == 0, result.stderr

    forbidden = sorted(
        path
        for path in result.stdout.splitlines()
        if path.startswith(PRIVATE_REPORT_PREFIXES)
    )
    assert not forbidden, (
        "Local engineering evidence must not be tracked in the public release "
        f"tree: {forbidden}"
    )
