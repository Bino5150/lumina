"""
Patch 3A.3A Part D / R9-R10 — proves tts/loader.py's module-level lock
actually serializes get_tts_backend() across real threads, not just that a
lock object exists in the source.

Uses a fake backend class monkeypatched in for tts.kokoro_bridge.KokoroBridge
so this never touches a GPU or a real model -- it only needs to prove the
critical section (old-instance observation -> old-load wait -> GPU cleanup
check -> construction -> singleton assignment) never overlaps between
concurrent callers.
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading
import time

import pytest

import config
import tts.kokoro_bridge as kokoro_bridge
import tts.loader as loader


@pytest.fixture(autouse=True)
def _reset_loader_singleton():
    prev_instance = loader._backend_instance
    prev_backend = getattr(config, "TTS_BACKEND", "kokoro")
    loader._backend_instance = None
    config.TTS_BACKEND = "kokoro"
    yield
    loader._backend_instance = prev_instance
    config.TTS_BACKEND = prev_backend


class _CountingFakeBackend:
    """Records concurrent construction entries; sleeps to widen the race
    window so an unserialized bug would reliably show max > 1."""

    _lock = threading.Lock()
    _current = 0
    _max = 0

    def __init__(self, hold_s=0.03):
        cls = _CountingFakeBackend
        with cls._lock:
            cls._current += 1
            cls._max = max(cls._max, cls._current)
        time.sleep(hold_s)
        with cls._lock:
            cls._current -= 1
        self._model = None
        self._load_thread = None

    @classmethod
    def reset(cls):
        cls._current = 0
        cls._max = 0


def test_loader_serializes_concurrent_force_reload_construction(monkeypatch):
    _CountingFakeBackend.reset()
    monkeypatch.setattr(kokoro_bridge, "KokoroBridge", _CountingFakeBackend)

    n_threads = 8
    results = [None] * n_threads

    def call(i):
        results[i] = loader.get_tts_backend(force_reload=True)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "a loader thread hung"
    assert _CountingFakeBackend._max == 1, (
        f"max concurrent construction entries was {_CountingFakeBackend._max}, "
        "expected exactly 1 -- the critical section overlapped"
    )
    # Each caller's return value is whatever _backend_instance was at the
    # end of *its own* serialized transition -- a real, fully-constructed
    # instance every time, never None or a half-built object, even though a
    # later caller's transition may have since replaced it as the global
    # singleton (that's expected: each transition fully completes before
    # the next one is allowed to start, not before every other caller reads
    # the result of theirs).
    assert all(isinstance(r, _CountingFakeBackend) for r in results)
    assert isinstance(loader._backend_instance, _CountingFakeBackend)


def test_loader_non_force_read_blocks_until_force_reload_transition_completes(monkeypatch):
    # Seed a real (fake) old instance first via an ordinary non-blocking call.
    monkeypatch.setattr(kokoro_bridge, "KokoroBridge", _CountingFakeBackend)
    old = loader.get_tts_backend(force_reload=False)
    assert isinstance(old, _CountingFakeBackend)

    entered = threading.Event()
    release = threading.Event()

    class _SlowFakeBackend:
        def __init__(self):
            entered.set()
            release.wait(timeout=10)
            self._model = None
            self._load_thread = None

    monkeypatch.setattr(kokoro_bridge, "KokoroBridge", _SlowFakeBackend)

    force_result = {}

    def do_force_reload():
        force_result["new"] = loader.get_tts_backend(force_reload=True)

    t_force = threading.Thread(target=do_force_reload)
    t_force.start()
    assert entered.wait(timeout=10), "force_reload construction never started"

    read_done = threading.Event()
    read_result = {}

    def do_read():
        read_result["got"] = loader.get_tts_backend(force_reload=False)
        read_done.set()

    t_read = threading.Thread(target=do_read)
    t_read.start()

    # The reader must be blocked by the lock while construction is in
    # flight -- it must not slip through and observe a half-replaced
    # singleton (or anything at all) before the transition finishes.
    time.sleep(0.2)
    assert not read_done.is_set(), (
        "get_tts_backend(force_reload=False) returned while a force_reload "
        "transition was still in flight -- it isn't actually serialized "
        "against the reload"
    )

    release.set()
    t_force.join(timeout=10)
    t_read.join(timeout=10)

    assert read_done.is_set()
    assert isinstance(force_result["new"], _SlowFakeBackend)
    # The unblocked read observes exactly the fully-constructed new
    # instance -- not the pre-transition old one, and not something
    # constructed independently by the reader itself.
    assert read_result["got"] is force_result["new"]
    assert read_result["got"] is not old
