"""
Voicebox TTS Bridge
Streams audio from Voicebox /generate/stream endpoint (v0.3.1+).
Plays via aplay. Non-blocking by default.
Voice profiles referenced by name — must match a profile in Voicebox.
"""
import requests
import threading
import queue
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from tts.base import BaseTTSBackend


class VoiceboxBridge(BaseTTSBackend):

    ENGINE = "chatterbox_turbo"      # daily driver — override per-call if needed
    LANGUAGE = "en"
    TIMEOUT = 300                    # generation can take 10-30s on CPU; stream starts earlier
    
    PHONETIC_MAP = {
        "Bino":   "Beeno",
        "Lumina": "Loo-mina",
}

    def __init__(self):
        super().__init__()
        self.host    = getattr(config, "VOICEBOX_HOST", "http://localhost:17493")
        self.profile = getattr(config, "VOICEBOX_PROFILE", "Lumina")
        self.instruct = getattr(config, "VOICEBOX_INSTRUCT", "")
        self.enabled = getattr(config, "TTS_ENABLED", True)

        # Profile name → ID cache (populated lazily on first list_voices call)
        self._profile_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def speak(self, text: str, blocking: bool = False, on_done=None,
              profile: str = None, instruct: str = None):
        """
        Speak text using the active Voicebox profile.
        profile  — override active profile name for this call
        instruct — optional delivery style, e.g. "warm, slightly amused"
                   (only effective with qwen_custom_voice engine)
        """
        if not self.enabled or not text.strip():
            return
        if blocking:
            self._speak_worker(text, on_done=on_done,
                               profile=profile, instruct=instruct)
        else:
            t = threading.Thread(
                target=self._speak_worker,
                args=(text,),
                kwargs={"on_done": on_done, "profile": profile, "instruct": instruct},
                daemon=True
            )
            t.start()

    def list_voices(self) -> list:
        """Return list of voice profile names from Voicebox."""
        try:
            resp = requests.get(f"{self.host}/profiles", timeout=5)
            if resp.status_code == 200:
                profiles = resp.json()
                # Refresh name → ID cache
                self._profile_cache = {p["name"]: p["id"] for p in profiles}
                return [p["name"] for p in profiles]
        except Exception as e:
            print(f"[VoiceboxBridge] list_voices error: {e}", file=sys.stderr)
        return []

    def test(self) -> bool:
        """Return True if Voicebox is reachable and healthy."""
        try:
            resp = requests.get(f"{self.host}/health", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    def set_profile(self, profile: str):
        """Set the active voice profile by name."""
        self.profile = profile

    def set_instruct(self, instruct: str):
        """Set delivery style instruction (Qwen CustomVoice only)."""
        self.instruct = instruct

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_profile_id(self, profile_name: str) -> str | None:
        """Resolve a profile name to its UUID. Refreshes cache if needed."""
        if profile_name not in self._profile_cache:
            self.list_voices()  # refresh
        return self._profile_cache.get(profile_name)

    def _speak_worker(self, text: str, on_done=None,
                    profile: str = None, instruct: str = None):
        import queue
        active_profile = profile or self.profile
        active_instruct = instruct or self.instruct

        profile_id = self._resolve_profile_id(active_profile)
        if not profile_id:
            print(f"[VoiceboxBridge] Profile '{active_profile}' not found in Voicebox",
                file=sys.stderr)
            return

        chunks = self._split_sentences(text, max_chars=500)
        chunks = [self._apply_phonetics(c) for c in chunks]
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return

        audio_queue = queue.Queue()
        SENTINEL = None  # signals producer is done

        def producer():
            for i, chunk in enumerate(chunks):
                payload = {
                    "profile_id": profile_id,
                    "text":       chunk,
                    "language":   self.LANGUAGE,
                    "engine":     self.ENGINE,
                }
                if active_instruct:
                    payload["instruct"] = active_instruct
                try:
                    with requests.post(
                        f"{self.host}/generate/stream",
                        json=payload,
                        timeout=self.TIMEOUT,
                        stream=True
                    ) as resp:
                        if resp.status_code == 200:
                            audio_bytes = b"".join(resp.iter_content(chunk_size=4096))
                            audio_queue.put(audio_bytes)
                            print(f"[VoiceboxBridge] chunk {i+1}/{len(chunks)} ready",
                                file=sys.stderr)
                        else:
                            print(f"[VoiceboxBridge] chunk {i+1} error {resp.status_code}",
                                file=sys.stderr)
                except Exception as e:
                    print(f"[VoiceboxBridge] chunk {i+1} failed: {e}", file=sys.stderr)
            audio_queue.put(SENTINEL)

        def consumer():
            while True:
                audio_bytes = audio_queue.get()
                if audio_bytes is SENTINEL:
                    break
                self._play_audio(audio_bytes)
            if on_done:
                on_done()

        # Start producer immediately, consumer plays as audio arrives
        prod = threading.Thread(target=producer, daemon=True)
        cons = threading.Thread(target=consumer, daemon=True)
        prod.start()
        cons.start()
        prod.join()
        cons.join()

    def _split_sentences(self, text: str, max_chars: int = 300) -> list:
        """Split text into sentence-sized chunks for sequential TTS."""
        import re
        # Strip Inner Thoughts blocks entirely
        text = re.sub(r'\[Inner Thoughts\].*?(?=---|Response:|$)', '', text, flags=re.DOTALL|re.IGNORECASE)
        # Strip section dividers and labels
        text = re.sub(r'\n?---\n?', ' ', text)
        text = re.sub(r'Response:\s*', '', text)
        # Strip markdown formatting
        text = re.sub(r'\*+', '', text)
        text = re.sub(r'#+\s*', '', text)
        text = re.sub(r'\[.*?\]', '', text)
        # Clean up extra whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        # Split on sentence boundaries
        sentences = re.split(r'(?<=[.!?])\s+', text.strip())
        chunks = []
        current = ""
        for s in sentences:
            if len(current) + len(s) + 1 <= max_chars:
                current = (current + " " + s).strip()
            else:
                if current:
                    chunks.append(current)
                current = s
        if current:
            chunks.append(current)
        return chunks
    
    def _apply_phonetics(self, text: str) -> str:
        import re
        for word, phonetic in self.PHONETIC_MAP.items():
            text = re.sub(rf'\b{word}\b', phonetic, text, flags=re.IGNORECASE)
        return text    