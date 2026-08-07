from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QComboBox, QLineEdit
from PySide6.QtCore import Signal

import os, sys, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _le, _btn, _scroll_wrap


# ── Tab: TTS ───────────────────────────────────────────────────────────────────

class TTSTab(QWidget):
    backend_changed = Signal(str)
    # Backend construction (get_tts_backend(force_reload=True)) now runs on a
    # worker thread -- loader.py may block it joining a previous in-flight
    # load thread, and _test_tts()/_save() run on the Qt main thread with no
    # existing worker dispatch, so a raw join() there would freeze the window.
    # These signals marshal the result back to the main thread for the
    # status label update, per Qt's cross-thread widget rule.
    _tts_test_result = Signal(bool, str)
    _tts_swap_done = Signal(str)

    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._tts_test_result.connect(self._on_tts_test_result)
        self._tts_swap_done.connect(self._on_tts_swap_done)
        self._build()

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)

        # ── TTS Backend ──
        layout.addWidget(_sec("TTS BACKEND", self.c))

        en_row = QHBoxLayout()
        self.enabled_cb = QCheckBox("Enable TTS")
        self.enabled_cb.setChecked(config.TTS_ENABLED)
        self.enabled_cb.setStyleSheet(f"color:{self.c['text_primary']};font-size:13px;background:transparent;")
        en_row.addWidget(self.enabled_cb)
        en_row.addStretch()
        layout.addLayout(en_row)

        backend_row = QHBoxLayout()
        backend_row.setSpacing(12)

        be_col = QVBoxLayout()
        be_col.addWidget(_lbl("Backend", self.c))
        self.tts_backend_combo = QComboBox()
        self.tts_backend_combo.addItems(["kokoro", "voicebox", "chatterbox", "supertonic", "elevenlabs", "piper"])
        self.tts_backend_combo.setCurrentText(getattr(config, "TTS_BACKEND", "kokoro"))
        self.tts_backend_combo.setFixedHeight(36)
        self.tts_backend_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        self.tts_backend_combo.currentTextChanged.connect(self._on_backend_changed)
        be_col.addWidget(self.tts_backend_combo)

        url_col = QVBoxLayout()
        url_col.addWidget(_lbl("Server URL", self.c))
        self.url = _le(config.TTS_HOST, self.c)
        url_col.addWidget(self.url)

        backend_row.addLayout(be_col, 1)
        backend_row.addLayout(url_col, 3)
        layout.addLayout(backend_row)

        # ── ElevenLabs API key (cloud backend -- no local host:port, so it
        # gets its own field instead of overloading the Server URL box) ──
        self.eleven_key_widget = QWidget()
        ek_layout = QHBoxLayout(self.eleven_key_widget)
        ek_layout.setContentsMargins(0, 4, 0, 0)
        ek_col = QVBoxLayout()
        ek_col.addWidget(_lbl("ElevenLabs API Key", self.c))
        self.eleven_key = _le(getattr(config, "ELEVENLABS_API_KEY", ""), self.c)
        self.eleven_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.eleven_key.setPlaceholderText("Your ElevenLabs API key")
        ek_col.addWidget(self.eleven_key)
        ek_layout.addLayout(ek_col)
        layout.addWidget(self.eleven_key_widget)
        self.eleven_key_widget.setVisible(self.tts_backend_combo.currentText() == "elevenlabs")

        layout.addWidget(_lbl("Voice settings (speed, pitch, volume) are now per-persona — configure them in the Personas tab.", self.c))

        # ── STT Backend ──
        layout.addWidget(_sec("STT BACKEND", self.c))

        stt_en_row = QHBoxLayout()
        self.stt_enabled_cb = QCheckBox("Enable STT (Push-to-Talk)")
        self.stt_enabled_cb.setChecked(getattr(config, "STT_ENABLED", True))
        self.stt_enabled_cb.setStyleSheet(f"color:{self.c['text_primary']};font-size:13px;background:transparent;")
        stt_en_row.addWidget(self.stt_enabled_cb)
        stt_en_row.addStretch()
        layout.addLayout(stt_en_row)

        stt_backend_row = QHBoxLayout()
        stt_backend_row.setSpacing(12)

        stt_be_col = QVBoxLayout()
        stt_be_col.addWidget(_lbl("Backend", self.c))
        self.stt_backend_combo = QComboBox()
        self.stt_backend_combo.addItems(["faster-whisper", "whisper"])
        self.stt_backend_combo.setCurrentText(getattr(config, "STT_BACKEND", "faster-whisper"))
        self.stt_backend_combo.setFixedHeight(36)
        self.stt_backend_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        stt_be_col.addWidget(self.stt_backend_combo)

        stt_model_col = QVBoxLayout()
        stt_model_col.addWidget(_lbl("Model Size", self.c))
        self.stt_model_combo = QComboBox()
        self.stt_model_combo.addItems(["tiny", "base", "small", "medium", "large-v2", "large-v3"])
        self.stt_model_combo.setCurrentText(getattr(config, "STT_MODEL", "base"))
        self.stt_model_combo.setFixedHeight(36)
        self.stt_model_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        stt_model_col.addWidget(self.stt_model_combo)

        stt_device_col = QVBoxLayout()
        stt_device_col.addWidget(_lbl("Device", self.c))
        self.stt_device_combo = QComboBox()
        self.stt_device_combo.addItems(["cpu", "cuda"])
        self.stt_device_combo.setCurrentText(getattr(config, "STT_DEVICE", "cpu"))
        self.stt_device_combo.setFixedHeight(36)
        self.stt_device_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        stt_device_col.addWidget(self.stt_device_combo)

        stt_backend_row.addLayout(stt_be_col, 2)
        stt_backend_row.addLayout(stt_model_col, 2)
        stt_backend_row.addLayout(stt_device_col, 1)
        layout.addLayout(stt_backend_row)

        stt_note = QLabel("Changes take effect on next Lumina restart.")
        stt_note.setStyleSheet(f"color:{self.c['text_dim']};font-size:11px;font-style:italic;background:transparent;")
        layout.addWidget(stt_note)

        # ── Save ──
        btn_row = QHBoxLayout()
        test_btn = _btn("▶ Test TTS", self.c)
        test_btn.clicked.connect(self._test_tts)
        save_btn = _btn("Save Settings", self.c, accent=True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(test_btn)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{self.c['text_muted']};font-size:11px;background:transparent;")
        layout.addWidget(self.status_lbl)
        layout.addStretch()

        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    _BACKEND_URLS = {
        "kokoro":   "http://localhost:8880",
        "voicebox": "http://localhost:17493",
        "chatterbox":  "http://localhost:8004",
        "supertonic":  "http://localhost:7788",
        "piper":    "http://localhost:5000",
    }

    def _on_backend_changed(self, name: str):
        is_cloud = (name == "elevenlabs")
        self.url.setText(self._BACKEND_URLS.get(name, ""))
        self.url.setEnabled(not is_cloud)
        self.eleven_key_widget.setVisible(is_cloud)
        if is_cloud:
            self.eleven_key.setText(getattr(config, "ELEVENLABS_API_KEY", ""))
        self.backend_changed.emit(name)
    def _fetch_voices(self):
        fallback = ["af_bella", "af_sarah", "af_nicole", "af_sky",
                    "am_adam", "am_michael", "bf_emma", "bf_isabella", "bf_lily"]
        try:
            import urllib.request, json
            host = config.TTS_HOST.rstrip("/")
            with urllib.request.urlopen(f"{host}/v1/audio/voices", timeout=3) as r:
                data = json.loads(r.read())
                voices = sorted(data.get("voices", []))
                return voices if voices else fallback
        except Exception:
            return fallback


    def _test_tts(self):
        self.status_lbl.setText("Testing...")
        import config as _c
        _c.TTS_BACKEND = self.tts_backend_combo.currentText()
        _c.TTS_HOST = self.url.text().strip()
        backend_name = _c.TTS_BACKEND
        # Only voicebox owns this URL field's meaning as VOICEBOX_HOST --
        # writing it unconditionally corrupted VOICEBOX_HOST (to whatever
        # _BACKEND_URLS default the currently-selected backend shows, e.g.
        # chatterbox's :8004) any time a non-voicebox backend was selected
        # at Apply-click time, and a later Save would persist that
        # corruption to prefs.json regardless of what was actually changed.
        if backend_name == "voicebox":
            _c.VOICEBOX_HOST = self.url.text().strip()
        elif backend_name == "elevenlabs":
            # Let "Test TTS" reflect the key as typed, before Save persists
            # it -- same live-test-before-save UX the VOICEBOX_HOST branch
            # above already gives Voicebox.
            _c.ELEVENLABS_API_KEY = self.eleven_key.text().strip()

        def worker():
            try:
                from tts.loader import get_tts_backend
                bridge = get_tts_backend(force_reload=True)
                bridge.enabled = True
                if bridge.test():
                    bridge.speak("Lumina TTS test successful.", blocking=False)
                    self._tts_test_result.emit(True, "✓ TTS server reachable — playing test audio.")
                else:
                    self._tts_test_result.emit(False, f"✗ TTS server not reachable. Is {backend_name} running?")
            except Exception as e:
                self._tts_test_result.emit(False, f"✗ Error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_tts_test_result(self, ok: bool, message: str):
        self.status_lbl.setText(message)
    def _save(self):
        config.TTS_ENABLED = self.enabled_cb.isChecked()
        config.TTS_BACKEND = self.tts_backend_combo.currentText()
        # ElevenLabs is cloud-only -- the URL field doesn't apply to it and
        # must not stomp TTS_HOST with an empty string, same corruption
        # class VOICEBOX_HOST already guards against elsewhere in this tab.
        if config.TTS_BACKEND != "elevenlabs":
            config.TTS_HOST = self.url.text().strip()
        if config.TTS_BACKEND == "elevenlabs":
            config.ELEVENLABS_API_KEY = self.eleven_key.text().strip()
            from core import secrets as _secrets
            _secrets.set_secret("elevenlabs_api_key", config.ELEVENLABS_API_KEY)
        config.STT_ENABLED = self.stt_enabled_cb.isChecked()
        config.STT_BACKEND = self.stt_backend_combo.currentText()
        config.STT_MODEL = self.stt_model_combo.currentText()
        config.STT_DEVICE = self.stt_device_combo.currentText()

        prefs = persistence.load()
        prefs["tts_enabled"] = config.TTS_ENABLED
        prefs["tts_host"] = config.TTS_HOST
        prefs["tts_backend"] = config.TTS_BACKEND
        prefs["voicebox_host"] = config.VOICEBOX_HOST
        prefs["voicebox_profile"] = config.VOICEBOX_PROFILE
        prefs["stt_enabled"] = config.STT_ENABLED
        prefs["stt_backend"] = config.STT_BACKEND
        prefs["stt_model"] = config.STT_MODEL
        prefs["stt_device"] = config.STT_DEVICE
        persistence.save(prefs)

        if self.agent.tts:
            self.agent.tts.enabled = config.TTS_ENABLED

            def worker():
                from tts.loader import get_tts_backend
                self.agent.tts = get_tts_backend(force_reload=True)
                self._tts_swap_done.emit("Settings saved.")

            self.status_lbl.setText("Settings saved (finishing TTS backend swap...)")
            threading.Thread(target=worker, daemon=True).start()
        else:
            self.status_lbl.setText("Settings saved.")

    def _on_tts_swap_done(self, message: str):
        self.status_lbl.setText(message)
