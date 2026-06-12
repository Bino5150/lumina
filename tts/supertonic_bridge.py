"""
SupertonicBridge — Supertonic 3 TTS backend.
99M param ONNX model, OpenAI-compatible API at localhost:7788.
Voice cloning via imported style JSONs — reference by name.
Nearly identical pattern to KokoroBridge.
"""
import requests
import threading
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from tts.base import BaseTTSBackend


class SupertonicBridge(BaseTTSBackend):

    def __init__(self):
        super().__init__()
        self.host    = getattr(config, "SUPERTONIC_HOST", "http://localhost:7788")
        self.voice   = getattr(config, "SUPERTONIC_VOICE", "default")
        self.enabled = getattr(config, "TTS_ENABLED", True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str, blocking: bool = False, on_done=None,
              voice: str = None):
        if not self.enabled or not text.strip():
            return
        if blocking:
            self._speak_worker(text, on_done=on_done, voice=voice)
        else:
            t = threading.Thread(
                target=self._speak_worker,
                args=(text,),
                kwargs={"on_done": on_done, "voice": voice},
                daemon=True
            )
            t.start()

    def list_voices(self) -> list:
        """Return list of available voice names from Supertonic."""
        try:
            resp = requests.get(f"{self.host}/v1/audio/voices", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                # OpenAI-compat format: {"voices": [{"voice_id": "...", "name": "..."}]}
                if "voices" in data:
                    return [v.get("name") or v.get("voice_id") for v in data["voices"]]
                # Flat list fallback
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[SupertonicBridge] list_voices error: {e}", file=sys.stderr)
        # Fallback: scan local custom styles cache
        styles_dir = os.path.expanduser("~/.cache/supertonic3/custom_styles/")
        if os.path.isdir(styles_dir):
            return [
                os.path.splitext(f)[0]
                for f in sorted(os.listdir(styles_dir))
                if f.endswith(".json")
            ]
        return []

    def test(self) -> bool:
        try:
            resp = requests.get(f"{self.host}/v1/audio/voices", timeout=3)
            return resp.status_code < 500
        except Exception:
            try:
                resp = requests.get(self.host, timeout=3)
                return True
            except Exception:
                return False

    def set_voice(self, voice: str):
        self.voice = voice

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _speak_worker(self, text: str, on_done=None, voice: str = None):
        active_voice = voice or self.voice
        try:
            resp = requests.post(
                f"{self.host}/v1/audio/speech",
                json={
                    "input": text,
                    "voice": active_voice,
                    "lang":  "en",
                    "response_format": "wav"
                },
                timeout=30
            )
            if resp.status_code == 200:
                self._play_audio(resp.content)
                if on_done:
                    on_done()
                return
            # Fallback: native endpoint
            resp = requests.post(
                f"{self.host}/v1/tts",
                json={"text": text, "voice": active_voice, "lang": "en"},
                timeout=30
            )
            if resp.status_code == 200:
                self._play_audio(resp.content)
                if on_done:
                    on_done()
                return
            print(f"[SupertonicBridge] Server returned {resp.status_code}",
                  file=sys.stderr)
        except requests.exceptions.ConnectionError:
            print(f"[SupertonicBridge] Not reachable at {self.host}", file=sys.stderr)
        except Exception as e:
            print(f"[SupertonicBridge] Error: {e}", file=sys.stderr)