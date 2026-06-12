"""
Whisper STT Bridge
Records from mic until silence, transcribes via faster-whisper (CPU).
"""

import threading
import tempfile
import os
import numpy as np

class WhisperBridge:
    def __init__(self, model_size: str = "base", device: str = "cpu"):
        self.model_size = model_size
        self.device = device
        self._model = None
        self._lock = threading.Lock()
        self.enabled = True

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            print(f"[STT] Loading Whisper {self.model_size} on {self.device}...", flush=True)
            self._model = WhisperModel(self.model_size, device=self.device, compute_type="int8")
            print("[STT] Whisper ready.", flush=True)

    def record_and_transcribe(self, on_done, on_error=None, silence_timeout: float = 2.0):
        """
        Record from mic until silence_timeout seconds of silence.
        Calls on_done(text) when transcription is ready.
        Non-blocking — runs in background thread.
        """
        t = threading.Thread(
            target=self._record_worker,
            args=(on_done, on_error, silence_timeout),
            daemon=True
        )
        t.start()

    def _record_worker(self, on_done, on_error, silence_timeout):
        try:
            import sounddevice as sd
            from scipy.io.wavfile import write as wav_write

            sample_rate = 16000
            chunk_duration = 0.5  # seconds per chunk
            chunk_samples = int(sample_rate * chunk_duration)
            silence_threshold = 0.01
            max_silence_chunks = int(silence_timeout / chunk_duration)

            print("[STT] Recording...", flush=True)
            audio_chunks = []
            silence_count = 0

            with sd.InputStream(samplerate=sample_rate, channels=1, dtype='float32') as stream:
                while True:
                    chunk, _ = stream.read(chunk_samples)
                    audio_chunks.append(chunk.copy())
                    rms = np.sqrt(np.mean(chunk**2))
                    if rms < silence_threshold:
                        silence_count += 1
                        if silence_count >= max_silence_chunks:
                            break
                    else:
                        silence_count = 0

            audio = np.concatenate(audio_chunks, axis=0)
            audio_int16 = (audio * 32767).astype(np.int16)

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_write(f, sample_rate, audio_int16)
                tmp_path = f.name

            print("[STT] Transcribing...", flush=True)
            self._load_model()
            segments, _ = self._model.transcribe(tmp_path, language="en")
            text = " ".join(seg.text for seg in segments).strip()
            print(f"[STT] Got: {text}", flush=True)

            try:
                os.unlink(tmp_path)
            except Exception:
                pass

            if text:
                on_done(text)

        except Exception as e:
            print(f"[STT] Error: {e}", flush=True)
            if on_error:
                on_error(str(e))

    def test(self) -> bool:
        try:
            self._load_model()
            return True
        except Exception:
            return False