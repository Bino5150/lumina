"""
ElevenLabsBridge — ElevenLabs cloud TTS backend.
REST API, voice referenced by opaque ID, no in-process model loading.
Nearly identical pattern to SupertonicBridge; name->ID voice cache follows
VoiceboxBridge._profile_cache.
"""
import requests
import threading
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from tts.base import BaseTTSBackend


class ElevenLabsBridge(BaseTTSBackend):

    BASE_URL = "https://api.elevenlabs.io/v1"

    PHONETIC_MAP = {
        "Bino":   "Beeno",
        "Lumina": "Loo-mina",
    }

    def __init__(self):
        super().__init__()
        self.api_key       = getattr(config, "ELEVENLABS_API_KEY", None)
        self.voice_id      = getattr(config, "ELEVENLABS_VOICE_ID", None)
        self.model_id      = getattr(config, "ELEVENLABS_MODEL", None)
        self.output_format = getattr(config, "ELEVENLABS_OUTPUT_FORMAT", None)
        self.enabled       = getattr(config, "TTS_ENABLED", True)

        # Voice name -> ID cache (populated lazily on first list_voices call)
        self._voice_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str, blocking: bool = False, on_done=None,
              voice_id: str = None, model_id: str = None):
        if not self.enabled or not text.strip():
            return
        if blocking:
            self._speak_worker(text, on_done=on_done, voice_id=voice_id, model_id=model_id)
        else:
            t = threading.Thread(
                target=self._speak_worker,
                args=(text,),
                kwargs={"on_done": on_done, "voice_id": voice_id, "model_id": model_id},
                daemon=True
            )
            t.start()

    def list_voices(self) -> list:
        """Return list of available voice names from ElevenLabs, refreshing the name->ID cache."""
        if not self.api_key:
            return []
        try:
            resp = requests.get(
                f"{self.BASE_URL}/voices",
                headers={"xi-api-key": self.api_key},
                timeout=5
            )
            if resp.status_code == 200:
                voices = resp.json().get("voices", [])
                self._voice_cache = {v["name"]: v["voice_id"] for v in voices}
                return [v["name"] for v in voices]
        except Exception as e:
            print(f"[ElevenLabsBridge] list_voices error: {e}", file=sys.stderr)
        return []

    def test(self) -> bool:
        """Cheap reachability + auth check — no generation credit spent."""
        if not self.api_key:
            return False
        try:
            resp = requests.get(
                f"{self.BASE_URL}/voices",
                headers={"xi-api-key": self.api_key},
                timeout=3
            )
            return resp.status_code == 200
        except Exception:
            return False

    def set_voice(self, voice_id: str):
        self.voice_id = voice_id

    def set_model(self, model_id: str):
        self.model_id = model_id

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_voice_id(self, voice) -> str | None:
        """Accept either a display name (from list_voices()) or a raw
        ElevenLabs voice ID and return the ID to use in the request.
        Falls back to self.voice_id when no per-call override is given --
        and resolves *that* too, since set_voice() (used by the Personas
        tab's voice dropdown) stores whatever it's handed, which is a
        display name, not an ID."""
        voice = voice or self.voice_id
        if not voice:
            return None
        if voice in self._voice_cache:
            return self._voice_cache[voice]
        if not self._voice_cache:
            self.list_voices()
            if voice in self._voice_cache:
                return self._voice_cache[voice]
        return voice  # not a known name -- assume it's already an ID

    def _speak_worker(self, text: str, on_done=None, voice_id: str = None, model_id: str = None):
        if not self.api_key:
            print("[ElevenLabsBridge] No API key configured, skipping.", file=sys.stderr)
            return

        active_voice = self._resolve_voice_id(voice_id)
        if not active_voice:
            print("[ElevenLabsBridge] No voice_id configured or resolved.", file=sys.stderr)
            return

        active_model = model_id or self.model_id
        payload = {"text": self._apply_phonetics(text)}
        if active_model:
            payload["model_id"] = active_model

        params = {"output_format": self.output_format} if self.output_format else {}

        try:
            resp = requests.post(
                f"{self.BASE_URL}/text-to-speech/{active_voice}",
                headers={"xi-api-key": self.api_key},
                params=params,
                json=payload,
                timeout=60
            )
            if resp.status_code == 200:
                self._play_audio(resp.content)
                if on_done:
                    on_done()
                return
            try:
                detail = resp.json().get("detail", {})
                err_msg = detail.get("message") if isinstance(detail, dict) else detail
            except Exception:
                err_msg = resp.text[:200]
            print(f"[ElevenLabsBridge] Server returned {resp.status_code}: {err_msg}",
                  file=sys.stderr)
        except requests.exceptions.ConnectionError:
            print("[ElevenLabsBridge] Not reachable", file=sys.stderr)
        except Exception as e:
            print(f"[ElevenLabsBridge] Error: {e}", file=sys.stderr)

    def _apply_phonetics(self, text: str) -> str:
        import re
        for word, phonetic in self.PHONETIC_MAP.items():
            text = re.sub(rf'\b{word}\b', phonetic, text, flags=re.IGNORECASE)
        return text
