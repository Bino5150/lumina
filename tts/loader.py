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

def get_tts_backend(force_reload: bool = False):
    global _backend_instance
    with _backend_lock:
        if _backend_instance is not None and not force_reload:
            return _backend_instance

        if force_reload and _backend_instance is not None:
            # If the backend being replaced has its own in-flight load thread
            # (e.g. ChatterboxBridge loading a model onto the GPU), wait it
            # out before starting a new one -- two concurrent loads racing
            # for the same GPU is what actually OOMs. This protects every
            # force_reload caller, not just Settings' Apply/Save sequence.
            old_thread = getattr(_backend_instance, "_load_thread", None)
            if old_thread is not None and old_thread.is_alive():
                print("[TTS] Waiting for previous backend's load to finish before replacing it...",
                      file=sys.stderr)
                old_thread.join()

            # Only now -- after the old load has settled, one way or the other --
            # do we know whether it actually finished (GPU-resident, needs
            # freeing) or failed (nothing to free). Checking before the join
            # above is useless: the old model doesn't exist yet at that point.
            old_model = getattr(_backend_instance, "_model", None)
            if old_model is not None and getattr(old_model, "device", None) == "cuda":
                # Set to None rather than del -- callers like ChatterboxBridge.test()
                # do `self._model is not None` and expect the attribute to exist.
                _backend_instance._model = None
                import torch
                torch.cuda.empty_cache()

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