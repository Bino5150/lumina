"""
TTS Backend Loader
Factory singleton for TTS backends — mirrors core/backends/loader.py pattern.
Swap backends at runtime by calling get_tts_backend(force_reload=True).
Supported backends (config.TTS_BACKEND):
  "kokoro"      — KokoroBridge      (OpenAI-compat FastAPI)
  "voicebox"    — VoiceboxBridge    (Voicebox local server, cloned voices)
  "chatterbox"  — ChatterboxBridge  (native in-process Chatterbox Turbo)
  "supertonic"  — SupertonicBridge  (Supertonic 3, OpenAI-compat at :7788)
  "piper"       — PiperBridge       (stub, offline, no deps)
"""
import sys
import config

_backend_instance = None

def get_tts_backend(force_reload: bool = False):
    global _backend_instance
    if _backend_instance is not None and not force_reload:
        return _backend_instance

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
    elif backend_name == "piper":
        from tts.piper_bridge import PiperBridge
        _backend_instance = PiperBridge()
    else:  # default: kokoro
        from tts.kokoro_bridge import KokoroBridge
        _backend_instance = KokoroBridge()

    print(f"[TTS] Backend loaded: {backend_name} → {type(_backend_instance).__name__}",
          file=sys.stderr)
    return _backend_instance