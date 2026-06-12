"""
Kokoro TTS Bridge
Sends text to the Kokoro server running on CPU via Voicebox/local HTTP.
Plays audio via aplay (Linux). Non-blocking.
"""

import requests
import threading
import tempfile
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config


class KokoroBridge:
    def __init__(self):
        self.host = config.TTS_HOST
        self.voice = config.TTS_VOICE
        self.speed = config.TTS_SPEED
        self.pitch = config.TTS_PITCH
        self.volume = config.TTS_VOLUME
        self.enabled = config.TTS_ENABLED
        self._lock = threading.Lock()
        self._current_proc = None

    def speak(self, text: str, blocking: bool = False, on_done=None):
        """Send text to Kokoro TTS. Non-blocking by default."""
        if not self.enabled or not text.strip():
            return
        if blocking:
            self._speak_worker(text, on_done=on_done)
        else:
            t = threading.Thread(target=self._speak_worker, args=(text,), kwargs={"on_done": on_done}, daemon=True)
            t.start()

    def _speak_worker(self, text: str, on_done=None):
        try:
            resp = requests.post(
                f"{self.host}/v1/audio/speech",
                json={
                    "model": "kokoro",
                    "input": text,
                    "voice": self.voice,
                    "speed": self.speed,
                    "pitch": self.pitch,
                    "volume_multiplier": self.volume,
                    "response_format": "wav"
                },
                timeout=30
            )
            if resp.status_code == 200:
                self._play_audio(resp.content)
                if on_done:
                    on_done()
                return

            # Fallback: try Voicebox-style endpoint
            resp = requests.post(
                f"{self.host}/tts",
                json={"text": text, "voice": self.voice, "speed": self.speed},
                timeout=30
            )
            if resp.status_code == 200:
                self._play_audio(resp.content)
                return

            print(f"[TTS] Server returned {resp.status_code}", file=sys.stderr)

        except requests.exceptions.ConnectionError:
            print(f"[TTS] Kokoro not reachable at {self.host}", file=sys.stderr)
        except Exception as e:
            print(f"[TTS] Error: {e}", file=sys.stderr)

    def _play_audio(self, audio_bytes: bytes):
        """Write to temp file and play with aplay."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name
        try:
            os.system(f"aplay -q '{tmp_path}'")
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def stop(self):
        """Stop current playback (best effort)."""
        os.system("pkill -x aplay 2>/dev/null")

    def set_voice(self, voice: str):
        self.voice = voice

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

    def test(self) -> bool:
        """Test if TTS server is reachable."""
        try:
            resp = requests.get(f"{self.host}/v1/models", timeout=3)
            return resp.status_code < 500
        except Exception:
            try:
                resp = requests.get(self.host, timeout=3)
                return True
            except Exception:
                return False
