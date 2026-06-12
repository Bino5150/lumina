"""
Piper TTS Bridge — STUB
Offline TTS via Piper (https://github.com/rhasspy/piper).
No Python deps — Piper runs as a subprocess binary.
TODO: implement when Piper binary is available on Skynet.
"""
import sys
from tts.base import BaseTTSBackend


class PiperBridge(BaseTTSBackend):

    def __init__(self):
        super().__init__()
        print("[PiperBridge] Piper backend not yet implemented — stub active", file=sys.stderr)

    def speak(self, text: str, blocking: bool = False, on_done=None):
        print("[PiperBridge] speak() called but Piper is not implemented yet", file=sys.stderr)

    def list_voices(self) -> list:
        return []

    def test(self) -> bool:
        return False
