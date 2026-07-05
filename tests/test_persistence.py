"""F-42: core/persistence.py — atomic write, save() reports failure instead
of swallowing it."""
import os
import json
import pytest


@pytest.fixture
def isolated_persistence(tmp_path, monkeypatch):
    """persistence.py binds PREFS_PATH at import time, so we monkeypatch the
    already-imported module's constant directly rather than fighting with
    config's import-time BASE_DIR resolution."""
    import core.persistence as persistence
    monkeypatch.setattr(persistence, "PREFS_PATH", str(tmp_path / "prefs.json"))
    return persistence


def test_save_returns_true_on_success(isolated_persistence):
    assert isolated_persistence.save({"foo": "bar"}) is True


def test_roundtrip(isolated_persistence):
    isolated_persistence.save({"foo": "bar", "n": 42})
    loaded = isolated_persistence.load()
    assert loaded["foo"] == "bar"
    assert loaded["n"] == 42


def test_defaults_merged_with_saved_values(isolated_persistence):
    isolated_persistence.save({"foo": "bar"})
    loaded = isolated_persistence.load()
    # a default we never set should still be present
    assert loaded["window_width"] == 1150


def test_no_leftover_tmp_file_after_save(isolated_persistence):
    isolated_persistence.save({"foo": "bar"})
    assert not os.path.exists(isolated_persistence.PREFS_PATH + ".tmp")


def test_load_missing_file_returns_defaults(isolated_persistence):
    # never saved anything — file doesn't exist
    loaded = isolated_persistence.load()
    assert loaded["avatar_path"] is None


def test_load_corrupted_json_falls_back_to_defaults(isolated_persistence):
    os.makedirs(os.path.dirname(isolated_persistence.PREFS_PATH), exist_ok=True)
    with open(isolated_persistence.PREFS_PATH, "w") as f:
        f.write("{not valid json!!!")
    loaded = isolated_persistence.load()
    assert loaded["window_width"] == 1150   # defaults, not a crash


def test_save_failure_returns_false_not_silent(isolated_persistence, monkeypatch, capsys):
    # force the write itself to fail
    def broken_open(*a, **kw):
        raise OSError("disk full (simulated)")
    monkeypatch.setattr("builtins.open", broken_open)

    result = isolated_persistence.save({"foo": "bar"})
    assert result is False
    # old code was a bare `except: pass` — this asserts it's now visible
    captured = capsys.readouterr()
    assert "PERSISTENCE" in captured.out
    assert "disk full" in captured.out


def test_atomic_write_leaves_old_file_intact_on_failure(isolated_persistence, monkeypatch):
    isolated_persistence.save({"original": "data"})

    real_open = open
    call_count = {"n": 0}

    def flaky_open(path, *a, **kw):
        # let the .tmp write start, then blow up before os.replace can run
        if str(path).endswith(".tmp"):
            call_count["n"] += 1
            raise OSError("simulated crash mid-write")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", flaky_open)
    isolated_persistence.save({"new": "data"})

    # original file must be untouched — os.replace never happened
    with open(isolated_persistence.PREFS_PATH) as f:
        data = json.load(f)
    assert data == {"original": "data"}
