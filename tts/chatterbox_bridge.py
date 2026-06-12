"""
ChatterboxBridge — Native in-process Chatterbox Turbo TTS.
Loads ChatterboxTurboTTS directly, no HTTP, no Docker.
Producer/consumer pipeline mirrors VoiceboxBridge.
"""
import threading
import queue
import sys
import os
import io
import re
import torch
import numpy as np

torch.set_num_threads(12)
torch.set_num_interop_threads(12)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from tts.base import BaseTTSBackend

SAMPLE_RATE = 24000


class ChatterboxBridge(BaseTTSBackend):

    PHONETIC_MAP = {
        "Bino":   "Beeno",
        "Lumina": "Loo-mina",
    }

    def __init__(self):
        super().__init__()
        self.enabled       = getattr(config, "TTS_ENABLED", True)
        self.reference_dir = getattr(config, "CHATTERBOX_REF_DIR",
                                     os.path.expanduser("~/chatterbox-tts-server/reference_audio"))
        self.voice         = getattr(config, "CHATTERBOX_VOICE", "lumina")
        self._model        = None
        self._model_lock   = threading.Lock()
        self._load_thread  = threading.Thread(target=self._load_model, daemon=True)
        self._load_thread.start()

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self):
        try:
            from chatterbox.tts_turbo import ChatterboxTurboTTS
            print("[ChatterboxBridge] Loading Turbo model...", flush=True)
            with self._model_lock:
                self._model = ChatterboxTurboTTS.from_pretrained(device="cpu")
            print("[ChatterboxBridge] Model ready.", flush=True)
        except Exception as e:
            print(f"[ChatterboxBridge] Model load failed: {e}", flush=True)

    def _wait_for_model(self, timeout=120) -> bool:
        self._load_thread.join(timeout=timeout)
        return self._model is not None

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
        """Return list of reference audio filenames (without extension)."""
        if not os.path.isdir(self.reference_dir):
            return []
        return [
            os.path.splitext(f)[0]
            for f in sorted(os.listdir(self.reference_dir))
            if f.lower().endswith((".wav", ".mp3"))
        ]

    def test(self) -> bool:
        return self._model is not None

    def set_voice(self, voice: str):
        self.voice = voice

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_ref_path(self, voice: str) -> str | None:
        """Resolve voice name to reference audio path."""
        for ext in (".wav", ".mp3"):
            path = os.path.join(self.reference_dir, voice + ext)
            if os.path.exists(path):
                return path
        print(f"[ChatterboxBridge] No reference audio for '{voice}'", flush=True)
        return None

    def _generate_chunk(self, text: str, ref_path: str | None) -> bytes:
        """Generate audio for one chunk, return WAV bytes."""
        import scipy.io.wavfile as wav_io
        with self._model_lock:
            if ref_path:
                wav_tensor = self._model.generate(text, audio_prompt_path=ref_path)
            else:
                wav_tensor = self._model.generate(text)
        audio_np = wav_tensor.squeeze().numpy()
        # Normalise to int16
        audio_np = np.clip(audio_np, -1.0, 1.0)
        audio_int16 = (audio_np * 32767).astype(np.int16)
        buf = io.BytesIO()
        wav_io.write(buf, SAMPLE_RATE, audio_int16)
        return buf.getvalue()

    def _speak_worker(self, text: str, on_done=None, voice: str = None):
        if not self._wait_for_model(timeout=120):
            print("[ChatterboxBridge] Model not ready, skipping.", flush=True)
            return

        active_voice = voice or self.voice
        ref_path = self._get_ref_path(active_voice)

        chunks = self._split_sentences(text, max_chars=200)
        chunks = [self._apply_phonetics(c) for c in chunks]
        chunks = [c for c in chunks if c.strip()]
        if not chunks:
            return

        audio_queue = queue.Queue()
        SENTINEL = None

        def producer():
            for i, chunk in enumerate(chunks):
                try:
                    audio_bytes = self._generate_chunk(chunk, ref_path)
                    audio_queue.put(audio_bytes)
                    print(f"[ChatterboxBridge] chunk {i+1}/{len(chunks)} ready",
                          flush=True)
                except Exception as e:
                    print(f"[ChatterboxBridge] chunk {i+1} failed: {e}", flush=True)
            audio_queue.put(SENTINEL)

        def consumer():
            while True:
                audio_bytes = audio_queue.get()
                if audio_bytes is SENTINEL:
                    break
                self._play_audio(audio_bytes)
            if on_done:
                on_done()

        prod = threading.Thread(target=producer, daemon=True)
        cons = threading.Thread(target=consumer, daemon=True)
        prod.start()
        cons.start()
        prod.join()
        cons.join()

    def _split_sentences(self, text: str, max_chars: int = 200) -> list:
        text = re.sub(r'\[Inner Thoughts\].*?(?=---|Response:|$)', '', text,
                      flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'\n?---\n?', ' ', text)
        text = re.sub(r'Response:\s*', '', text)
        text = re.sub(r'\*+', '', text)
        text = re.sub(r'#+\s*', '', text)
        # Preserve paralinguistic tags like [laugh], [sigh] etc.
        text = re.sub(r'\[(?!laugh|chuckle|sigh|gasp|cough|clear throat|sniff|groan|shush)[^\]]*\]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
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
        for word, phonetic in self.PHONETIC_MAP.items():
            text = re.sub(rf'\b{word}\b', phonetic, text, flags=re.IGNORECASE)
        return text