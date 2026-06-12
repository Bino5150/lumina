"""
BaseTTSBackend — abstract interface for all TTS bridges.
All backends must implement speak(), list_voices(), and test().
"""
import threading
import os
import tempfile
from abc import ABC, abstractmethod


class BaseTTSBackend(ABC):

    def __init__(self):
        self.enabled = True
        self._lock = threading.Lock()

    @abstractmethod
    def speak(self, text: str, blocking: bool = False, on_done=None):
        """Speak text. Non-blocking by default."""
        pass

    @abstractmethod
    def list_voices(self) -> list:
        """Return list of available voice names/IDs."""
        pass

    @abstractmethod
    def test(self) -> bool:
        """Return True if backend is reachable."""
        pass

    def stop(self):
        """Stop current playback. Override if backend supports it."""
        os.system("pkill -x aplay 2>/dev/null")

    def set_enabled(self, enabled: bool):
        self.enabled = enabled

    def _play_audio(self, audio_bytes: bytes):
        """Write WAV bytes to temp file and play with aplay."""
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
