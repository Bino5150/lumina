"""tools/updates.py — MB-04: check_for_updates() must never pull/merge, only
git fetch + rev-list comparisons. Mocks subprocess.run so CI never hits the
real network, and asserts the up-to-date / behind / fetch-failure messages,
plus that nothing resembling a pull/merge is ever invoked.
"""
from unittest.mock import patch, MagicMock

from tools.updates import check_for_updates


def _run_side_effect(responses):
    def _run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        for key, (returncode, stdout) in responses.items():
            if tuple(cmd[:len(key)]) == key:
                result.returncode = returncode
                result.stdout = stdout
                return result
        return result
    return _run


def test_up_to_date():
    responses = {
        ("git", "fetch", "--quiet"): (0, ""),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main"),
        ("git", "rev-list", "--count", "HEAD..origin/main"): (0, "0"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): (0, "0"),
    }
    with patch("subprocess.run", side_effect=_run_side_effect(responses)):
        result = check_for_updates()
    assert "Up to date" in result


def test_behind_reports_count_and_never_pulls():
    responses = {
        ("git", "fetch", "--quiet"): (0, ""),
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): (0, "main"),
        ("git", "rev-list", "--count", "HEAD..origin/main"): (0, "3"),
        ("git", "rev-list", "--count", "origin/main..HEAD"): (0, "0"),
    }
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return _run_side_effect(responses)(cmd, **kwargs)

    with patch("subprocess.run", side_effect=_run) as mock_run:
        result = check_for_updates()

    assert "3 commit(s) behind" in result
    for cmd in calls:
        assert "pull" not in cmd
        assert "merge" not in cmd


def test_fetch_failure_returns_message_not_exception():
    responses = {
        ("git", "fetch", "--quiet"): (1, ""),
    }
    with patch("subprocess.run", side_effect=_run_side_effect(responses)):
        result = check_for_updates()
    assert "Update check failed" in result
