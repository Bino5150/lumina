"""
TTS Backend Loader
Factory singleton for TTS backends — mirrors core/backends/loader.py pattern.
Swap backends at runtime by calling get_tts_backend(force_reload=True).
Supported backends (config.TTS_BACKEND):
  "kokoro"      — KokoroBridge      (OpenAI-compat FastAPI)
  "voicebox"    — VoiceboxBridge    (Voicebox local server, cloned voices)
  "chatterbox"  — ChatterboxBridge  (native in-process Chatterbox Turbo)
  "supertonic"  — SupertonicBridge  (Supertonic 3, OpenAI-compat at :7788)
  "elevenlabs"  — ElevenLabsBridge  (ElevenLabs cloud REST API)
  "piper"       — PiperBridge       (stub, offline, no deps)
"""
import sys
import threading
import config

_backend_instance = None
# Actually serializes construction at the source (the comments below used to
# claim this and didn't -- nothing stopped two callers from both observing
# the same old _backend_instance and racing each other through GPU cleanup
# and construction). RLock, not Lock: backend construction below can run
# arbitrary bridge __init__ code, and a plain Lock would deadlock outright
# if any of that ever re-enters get_tts_backend() on the same thread, where
# an RLock just no-ops. The whole function body runs under this lock as one
# transition so a non-force caller can never observe a half-replaced
# singleton -- it either gets the pre-transition instance (lock not yet
# taken by a reloader) or blocks until the new one is fully constructed and
# assigned, never something in between.
_backend_lock = threading.RLock()


def _dispose_backend(backend):
    """Best-effort teardown for one detached backend.

    Every concrete backend has stop() directly or through BaseTTSBackend.
    Chatterbox additionally owns an asynchronous model load and possibly a
    CUDA model; preserve the loader's existing replacement cleanup for both
    explicit unload and force-reload.
    """
    if backend is None:
        return

    set_enabled = getattr(backend, "set_enabled", None)
    try:
        if callable(set_enabled):
            set_enabled(False)
        elif hasattr(backend, "enabled"):
            backend.enabled = False
    except Exception as exc:
        print(f"[TTS] Failed to disable detached backend: {exc}", file=sys.stderr)

    stop = getattr(backend, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception as exc:
            print(f"[TTS] Failed to stop detached backend: {exc}", file=sys.stderr)

    load_thread = getattr(backend, "_load_thread", None)
    if load_thread is not None and load_thread.is_alive():
        print("[TTS] Waiting for backend model load before release...", file=sys.stderr)
        load_thread.join()

    model = getattr(backend, "_model", None)
    if model is not None and getattr(model, "device", None) == "cuda":
        backend._model = None
        import torch
        torch.cuda.empty_cache()


def unload_tts_backend(backend=None) -> bool:
    """Stop/release a backend and clear the loader singleton when applicable.

    Passing None unloads the current singleton. Passing an explicitly detached
    agent backend also supports shutdown/tests where it is not the singleton.
    Returns whether a backend existed to release.
    """
    global _backend_instance
    with _backend_lock:
        target = backend if backend is not None else _backend_instance
        if target is None:
            return False
        if target is _backend_instance:
            _backend_instance = None
        _dispose_backend(target)
        return True


def get_tts_backend(force_reload: bool = False):
    global _backend_instance
    with _backend_lock:
        if _backend_instance is not None and not force_reload:
            return _backend_instance

        if force_reload and _backend_instance is not None:
            old_backend = _backend_instance
            _backend_instance = None
            _dispose_backend(old_backend)

        backend_name = getattr(config, "TTS_BACKEND", "kokoro").lower().strip()

        if backend_name == "voicebox":
            from tts.voicebox_bridge import VoiceboxBridge
            _backend_instance = VoiceboxBridge()
        elif backend_name == "chatterbox":
            from tts.chatterbox_bridge import ChatterboxBridge
            _backend_instance = ChatterboxBridge()
        elif backend_name == "supertonic":
            from tts.supertonic_bridge import SupertonicBridge
            _backend_instance = SupertonicBridge()
        elif backend_name == "elevenlabs":
            from tts.elevenlabs_bridge import ElevenLabsBridge
            _backend_instance = ElevenLabsBridge()
        elif backend_name == "piper":
            from tts.piper_bridge import PiperBridge
            _backend_instance = PiperBridge()
        else:  # default: kokoro
            from tts.kokoro_bridge import KokoroBridge
            _backend_instance = KokoroBridge()

        print(f"[TTS] Backend loaded: {backend_name} → {type(_backend_instance).__name__}",
              file=sys.stderr)
        return _backend_instance
