from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QLineEdit, QComboBox
from PySide6.QtCore import Signal, QTimer

import os, sys, threading
from typing import Optional
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _le, _btn, _combo, _scroll_wrap, ButtonFeedback, safe_error_detail

# MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 (CI repair): the Higgsfield
# display-only label map and pricing URL live in ui.multimodal_display --
# a Qt-free module outside the ui.settings package -- specifically so pure
# data/constant tests can import them without pulling in PySide6 (importing
# ANY ui.settings submodule runs ui/settings/__init__.py first, which
# unconditionally imports every Settings tab). Re-imported under their
# original private names here so nothing else in this file needs to change.
from ui.multimodal_display import (
    HIGGSFIELD_MODEL_LABELS as _HIGGSFIELD_MODEL_LABELS,
    HIGGSFIELD_PRICING_URL as _HIGGSFIELD_PRICING_URL,
    higgsfield_model_label as _higgsfield_model_label,
)


# ── Tab: Multimodal ────────────────────────────────────────────────────────────

class MultimodalTab(QWidget):
    backend_changed = Signal(str)
    # Backend construction (get_tts_backend(force_reload=True)) now runs on a
    # worker thread -- loader.py may block it joining a previous in-flight
    # load thread, and _test_tts()/_save() run on the Qt main thread with no
    # existing worker dispatch, so a raw join() there would freeze the window.
    # These signals marshal the result back to the main thread for the
    # status label update, per Qt's cross-thread widget rule.
    _tts_test_result = Signal(bool, str)
    _tts_swap_done = Signal(bool, str)

    _RESULT_HOLD_MS = 1750  # ~1.5-2s truthful-outcome display before button text reverts

    # Same established backend vocabulary exposed by General Settings and
    # consumed by core.backends.loader.  Keeping the list data-only avoids
    # importing/constructing providers merely because Settings was opened.
    _VISION_PROVIDERS = (
        "llamacpp", "lmstudio", "ollama", "vllm", "openrouter",
        "deepseek", "groq", "openai", "anthropic", "gemini", "kimi",
        "qwen", "custom", "omniroute",
    )

    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        # Single-flight gate shared by Test and Save: guards entry so a
        # second Test/Save activation -- from a rapid double-click *or* a
        # direct method call that bypasses the button entirely -- can't start
        # a second concurrent backend reload. setEnabled(False) alone only
        # blocks the click path, not direct invocation, so busy is checked
        # and set atomically under _op_lock at the top of both entry points.
        self._op_lock = threading.Lock()
        self._busy = False
        # Bumped by _try_enter_busy() every time a new operation is
        # accepted. A delayed button-text-revert timer (see
        # _schedule_feedback_reset) captures the generation in flight when
        # it's scheduled and checks it again when it fires -- if a newer
        # operation has since started, the generation has moved on and the
        # stale callback is a no-op instead of stomping the new operation's
        # button text.
        self._feedback_generation = 0
        # VISION-RESTORE-01: last successful live model discovery per
        # specialist provider (same shape as General Settings'
        # _discovered_models). Populated only by an explicit ⟳ click, never
        # by passive construction.
        self._vision_discovered_models: dict[str, tuple[str, ...]] = {}
        self._tts_test_result.connect(self._on_tts_test_result)
        self._tts_swap_done.connect(self._on_tts_swap_done)
        self._build()

    def _try_enter_busy(self) -> bool:
        with self._op_lock:
            if self._busy:
                return False
            self._busy = True
            self._feedback_generation += 1
            return True

    def _leave_busy(self):
        with self._op_lock:
            self._busy = False
        self.save_btn.setEnabled(True)
        self._sync_tts_test_enabled()

    def _sync_tts_test_enabled(self):
        if hasattr(self, "test_btn"):
            self.test_btn.setEnabled(
                not self._busy
                and bool(getattr(config, "TTS_ENABLED", True))
                and self.enabled_cb.isChecked()
            )

    def _attach_backend(self, backend):
        attach = getattr(self.agent, "attach_tts_backend", None)
        if callable(attach):
            attach(backend)
        else:
            self.agent.tts = backend

    @staticmethod
    def _apply_live_backend_config(backend):
        if backend is None:
            return
        set_enabled = getattr(backend, "set_enabled", None)
        if callable(set_enabled):
            set_enabled(True)
        elif hasattr(backend, "enabled"):
            backend.enabled = True

        backend_name = getattr(config, "TTS_BACKEND", "kokoro")
        if backend_name == "voicebox" and hasattr(backend, "host"):
            backend.host = config.VOICEBOX_HOST
        elif backend_name != "elevenlabs" and hasattr(backend, "host"):
            backend.host = config.TTS_HOST
        if backend_name == "elevenlabs" and hasattr(backend, "api_key"):
            backend.api_key = config.ELEVENLABS_API_KEY

    def _schedule_feedback_reset(self, fn):
        """Delay-run fn() after _RESULT_HOLD_MS, but only if no newer
        operation has been accepted in the meantime. Callers invoke this
        while still inside the generation that owns the reset -- capturing
        self._feedback_generation now is always the right value, since
        _try_enter_busy() can't bump it again until this operation calls
        _leave_busy()."""
        generation = self._feedback_generation

        def guarded():
            if generation == self._feedback_generation:
                fn()

        QTimer.singleShot(self._RESULT_HOLD_MS, guarded)

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)

        layout.addWidget(_sec("VOICE I/O", self.c))

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
        self.tts_backend_combo = _combo(self.c)
        self.tts_backend_combo.addItems(["kokoro", "voicebox", "chatterbox", "supertonic", "elevenlabs", "piper"])
        self.tts_backend_combo.setCurrentText(getattr(config, "TTS_BACKEND", "kokoro"))
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
        self.stt_backend_combo = _combo(self.c)
        self.stt_backend_combo.addItems(["faster-whisper", "whisper"])
        self.stt_backend_combo.setCurrentText(getattr(config, "STT_BACKEND", "faster-whisper"))
        stt_be_col.addWidget(self.stt_backend_combo)

        stt_model_col = QVBoxLayout()
        stt_model_col.addWidget(_lbl("Model Size", self.c))
        self.stt_model_combo = _combo(self.c)
        self.stt_model_combo.addItems(["tiny", "base", "small", "medium", "large-v2", "large-v3"])
        self.stt_model_combo.setCurrentText(getattr(config, "STT_MODEL", "base"))
        stt_model_col.addWidget(self.stt_model_combo)

        stt_device_col = QVBoxLayout()
        stt_device_col.addWidget(_lbl("Device", self.c))
        self.stt_device_combo = _combo(self.c)
        self.stt_device_combo.addItems(["cpu", "cuda"])
        self.stt_device_combo.setCurrentText(getattr(config, "STT_DEVICE", "cpu"))
        stt_device_col.addWidget(self.stt_device_combo)

        stt_backend_row.addLayout(stt_be_col, 2)
        stt_backend_row.addLayout(stt_model_col, 2)
        stt_backend_row.addLayout(stt_device_col, 1)
        layout.addLayout(stt_backend_row)

        stt_note = QLabel("Changes take effect on next Lumina restart.")
        stt_note.setStyleSheet(f"color:{self.c['text_dim']};font-size:11px;font-style:italic;background:transparent;")
        layout.addWidget(stt_note)

        # ── Existing M1/M2 routed-vision policy ──
        # This is configuration only.  Passive construction performs no
        # provider discovery, backend construction, model call, or media I/O.
        layout.addWidget(_sec("VISION / IMAGE UNDERSTANDING", self.c))
        vision_note = _lbl(
            "Configure the existing M1 capability route used by the bounded "
            "M2 vision lane. The primary conversation backend is never "
            "switched by this policy.",
            self.c,
        )
        vision_note.setWordWrap(True)
        layout.addWidget(vision_note)

        from core.capability_router import parse_routes

        raw_routes = getattr(config, "MULTIMODAL_ROUTES", {})
        parsed_routes, route_warnings = parse_routes(raw_routes)
        route = parsed_routes.get("vision_understanding")
        raw_vision = (
            raw_routes.get("vision_understanding", {})
            if isinstance(raw_routes, dict) else {}
        )
        self._vision_route_present = (
            isinstance(raw_routes, dict)
            and "vision_understanding" in raw_routes
        )
        self._vision_route_template = (
            dict(raw_vision) if isinstance(raw_vision, dict) else {}
        )

        route_row = QHBoxLayout()
        route_row.setSpacing(12)

        mode_col = QVBoxLayout()
        mode_col.addWidget(_lbl("Route mode", self.c))
        self.vision_mode_combo = _combo(self.c)
        self.vision_mode_combo.addItem("Disabled", "disabled")
        self.vision_mode_combo.addItem("Auto", "auto")
        self.vision_mode_combo.addItem("Specialist", "specialist")
        mode = route.mode if route is not None else "disabled"
        self.vision_mode_combo.setCurrentIndex(
            self.vision_mode_combo.findData(mode)
        )
        mode_col.addWidget(self.vision_mode_combo)

        provider_col = QVBoxLayout()
        provider_col.addWidget(_lbl("Preferred specialist provider", self.c))
        self.vision_provider_combo = _combo(self.c)
        self.vision_provider_combo.addItem("")
        self.vision_provider_combo.addItems(list(self._VISION_PROVIDERS))
        configured_provider = route.specialist if route is not None else None
        if configured_provider and self.vision_provider_combo.findText(configured_provider) < 0:
            # Preserve owner-authored provider identities even if the current
            # UI catalog does not know them; the label below does not claim
            # support or perform discovery.
            self.vision_provider_combo.addItem(configured_provider)
        self.vision_provider_combo.setCurrentText(configured_provider or "")
        provider_col.addWidget(self.vision_provider_combo)

        route_row.addLayout(mode_col, 1)
        route_row.addLayout(provider_col, 2)
        layout.addLayout(route_row)

        fallback_row = QHBoxLayout()
        fallback_row.setSpacing(12)

        fallback_col = QVBoxLayout()
        fallback_col.addWidget(_lbl("Owner-permitted fallbacks (comma-separated)", self.c))
        self.vision_fallbacks = _le(
            ", ".join(route.fallbacks) if route is not None else "", self.c
        )
        fallback_col.addWidget(self.vision_fallbacks)

        disabled_col = QVBoxLayout()
        disabled_col.addWidget(_lbl("Disabled specialist providers (comma-separated)", self.c))
        self.vision_disabled_providers = _le(
            ", ".join(getattr(config, "MULTIMODAL_DISABLED_PROVIDERS", []) or ()),
            self.c,
        )
        disabled_col.addWidget(self.vision_disabled_providers)

        fallback_row.addLayout(fallback_col, 1)
        fallback_row.addLayout(disabled_col, 1)
        layout.addLayout(fallback_row)

        model_col = QVBoxLayout()
        model_col.addWidget(_lbl("Model", self.c))
        self.vision_model_combo = _combo(self.c)
        self.vision_model_combo.setEditable(True)
        # VISION-RESTORE-01: Enter must not append a data-less duplicate
        # item; the typed text itself is the override (see
        # _vision_model_override()).
        self.vision_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.vision_model_combo.addItem("Provider default", "")
        configured_model_override = (route.model or "") if route is not None else ""
        if configured_model_override:
            self.vision_model_combo.addItem(configured_model_override, configured_model_override)
        vision_model_row = QHBoxLayout()
        vision_model_row.setSpacing(6)
        vision_model_row.addWidget(self.vision_model_combo, 1)
        self.vision_refresh_models_btn = _btn("⟳", self.c)
        self.vision_refresh_models_btn.setFixedWidth(36)
        self.vision_refresh_models_btn.setToolTip(
            "Fetch this provider's models using its saved General Settings "
            "credentials, without changing the selected model"
        )
        self.vision_refresh_models_btn.clicked.connect(self._refresh_vision_models)
        vision_model_row.addWidget(self.vision_refresh_models_btn)
        model_col.addLayout(vision_model_row)
        layout.addLayout(model_col)

        model_note = _lbl(
            "Provider default follows that provider's own configured model "
            "(General Settings). Type a specific model id, or ⟳ to list the "
            "provider's models, to override it for vision_understanding "
            "only -- the primary conversation "
            "backend/model is never changed by this selection.",
            self.c,
        )
        model_note.setWordWrap(True)
        layout.addWidget(model_note)
        if route_warnings:
            warning = _lbl("Invalid stored route is fail-closed: " + "; ".join(route_warnings), self.c)
            warning.setWordWrap(True)
            layout.addWidget(warning)

        self.vision_mode_combo.currentIndexChanged.connect(self._sync_vision_controls)
        self.vision_provider_combo.currentTextChanged.connect(self._refresh_vision_model)
        if configured_model_override:
            self.vision_model_combo.setCurrentIndex(1)
        self._sync_vision_controls()
        self._refresh_vision_model()

        # ── Image Generation -- provider/model binding + Higgsfield
        # credentials ──
        # MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 adds the provider/model
        # binding below; credentials remain in this same section per that
        # campaign's own scope note (moving them would expand scope
        # unnecessarily). Higgsfield is not a conversational backend, so --
        # unlike the vision route above, which reuses an existing chat
        # provider's own API key field -- there is no chat-backend
        # credential slot to piggyback on here. Credential values are
        # read/written only through core.secrets (see that module's
        # docstring) -- never prefs.json, never config.py, never logged,
        # never re-displayed in plaintext.
        layout.addWidget(_sec("IMAGE GENERATION", self.c))

        from core.higgsfield_adapter import SUPPORTED_MODELS as _HF_SUPPORTED_MODELS

        raw_image_route = (
            raw_routes.get("image_generation", {})
            if isinstance(raw_routes, dict) else {}
        )
        image_route = parsed_routes.get("image_generation")
        configured_image_model = (
            (image_route.model or "") if image_route is not None else ""
        )
        self._image_route_template = (
            dict(raw_image_route) if isinstance(raw_image_route, dict) else {}
        )
        # MULTIMODAL-PER-CAPABILITY-MODEL-BINDING-01 (post-review fix):
        # absence of an image_generation route must stay absence until the
        # owner deliberately configures this capability -- opening Settings
        # and saving an UNRELATED field must never silently admit a new,
        # potentially cost-bearing route. Mirrors vision_understanding's own
        # `_vision_route_present` contract exactly.
        self._image_route_present = (
            isinstance(raw_routes, dict) and "image_generation" in raw_routes
        )

        image_mode_row = QHBoxLayout()
        image_mode_col = QVBoxLayout()
        image_mode_col.addWidget(_lbl("Route mode", self.c))
        self.image_mode_combo = _combo(self.c)
        self.image_mode_combo.addItem("Disabled", "disabled")
        self.image_mode_combo.addItem("Specialist", "specialist")
        image_mode = image_route.mode if image_route is not None else "disabled"
        self.image_mode_combo.setCurrentIndex(self.image_mode_combo.findData(image_mode))
        image_mode_col.addWidget(self.image_mode_combo)
        image_mode_row.addLayout(image_mode_col, 1)
        image_mode_row.addStretch(2)
        layout.addLayout(image_mode_row)

        image_row = QHBoxLayout()
        image_row.setSpacing(12)

        image_provider_col = QVBoxLayout()
        image_provider_col.addWidget(_lbl("Provider", self.c))
        self.image_provider_combo = _combo(self.c)
        self.image_provider_combo.addItem("Higgsfield", "higgsfield")
        image_provider_col.addWidget(self.image_provider_combo)

        image_model_col = QVBoxLayout()
        image_model_col.addWidget(_lbl("Model", self.c))
        self.image_model_combo = _combo(self.c)
        for model_id in _HF_SUPPORTED_MODELS:
            self.image_model_combo.addItem(_higgsfield_model_label(model_id), model_id)
        image_model_index = 0
        if configured_image_model:
            found = self.image_model_combo.findData(configured_image_model)
            if found >= 0:
                image_model_index = found
            else:
                # Preserve a stale/owner-authored id rather than silently
                # dropping it -- fail-closed resolution happens at
                # execution time (core.image_generation_service.
                # resolve_image_generation_target()), never here.
                self.image_model_combo.addItem(configured_image_model, configured_image_model)
                image_model_index = self.image_model_combo.count() - 1
        self.image_model_combo.setCurrentIndex(image_model_index)
        image_model_col.addWidget(self.image_model_combo)

        image_row.addLayout(image_provider_col, 1)
        image_row.addLayout(image_model_col, 2)
        layout.addLayout(image_row)

        image_pricing_row = QHBoxLayout()
        self.image_pricing_btn = _btn("View Higgsfield pricing", self.c)
        self.image_pricing_btn.clicked.connect(
            lambda: __import__("webbrowser").open(_HIGGSFIELD_PRICING_URL)
        )
        image_pricing_row.addWidget(self.image_pricing_btn)
        image_pricing_row.addStretch()
        layout.addLayout(image_pricing_row)

        image_pricing_note = _lbl(
            "Opens Higgsfield's own current pricing/model page. Lumina's "
            "local cost estimate (used for the spend gate) is informational "
            "only -- the provider's page is the authoritative, current price.",
            self.c,
        )
        image_pricing_note.setWordWrap(True)
        layout.addWidget(image_pricing_note)

        self.image_mode_combo.currentIndexChanged.connect(self._sync_image_controls)
        self._sync_image_controls()

        hf_note = _lbl(
            "API credentials for the Higgsfield image-generation adapter. "
            "Stored in Lumina's OS-local credential store, separate from "
            "prefs.json and from any conversational backend configuration.",
            self.c,
        )
        hf_note.setWordWrap(True)
        layout.addWidget(hf_note)

        self.hf_status_lbl = _lbl("", self.c)
        layout.addWidget(self.hf_status_lbl)

        hf_id_col = QVBoxLayout()
        hf_id_col.addWidget(_lbl("Key ID", self.c))
        self.hf_key_id = _le("", self.c)
        self.hf_key_id.setEchoMode(QLineEdit.EchoMode.Password)
        hf_id_col.addWidget(self.hf_key_id)
        layout.addLayout(hf_id_col)

        hf_secret_col = QVBoxLayout()
        hf_secret_col.addWidget(_lbl("Key Secret", self.c))
        self.hf_key_secret = _le("", self.c)
        self.hf_key_secret.setEchoMode(QLineEdit.EchoMode.Password)
        hf_secret_col.addWidget(self.hf_key_secret)
        layout.addLayout(hf_secret_col)

        hf_save_row = QHBoxLayout()
        self.hf_save_btn = _btn("Save", self.c)
        self.hf_save_btn.clicked.connect(self._save_higgsfield_credentials)
        hf_save_row.addStretch()
        hf_save_row.addWidget(self.hf_save_btn)
        layout.addLayout(hf_save_row)
        self._hf_feedback = ButtonFeedback(self.hf_save_btn)

        self.hf_save_status_lbl = _lbl("", self.c)
        self.hf_save_status_lbl.setWordWrap(True)
        layout.addWidget(self.hf_save_status_lbl)

        self._refresh_higgsfield_status()

        # ── Save ──
        btn_row = QHBoxLayout()
        self.test_btn = _btn("▶ Test TTS", self.c)
        self.test_btn.clicked.connect(self._test_tts)
        self.save_btn = _btn("Save Settings", self.c, accent=True)
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.test_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)
        self.enabled_cb.toggled.connect(self._sync_tts_test_enabled)
        self._sync_tts_test_enabled()

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{self.c['text_muted']};font-size:11px;background:transparent;")
        layout.addWidget(self.status_lbl)
        layout.addStretch()

        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    @staticmethod
    def _csv_values(text: str) -> list[str]:
        """Ordered, de-duplicated non-empty values from a compact UI field."""
        values = []
        for value in (part.strip() for part in text.split(",")):
            if value and value not in values:
                values.append(value)
        return values

    def _configured_vision_model(self, provider: str) -> str:
        if not provider:
            return "No specialist provider configured"
        model_attrs = {
            "openrouter": "OPENROUTER_DEFAULT_MODEL",
            "deepseek": "DEEPSEEK_DEFAULT_MODEL",
            "groq": "GROQ_DEFAULT_MODEL",
            "openai": "OPENAI_DEFAULT_MODEL",
            "anthropic": "ANTHROPIC_DEFAULT_MODEL",
            "gemini": "GEMINI_DEFAULT_MODEL",
            "kimi": "KIMI_DEFAULT_MODEL",
            "qwen": "QWEN_DEFAULT_MODEL",
            "custom": "CUSTOM_DEFAULT_MODEL",
            "omniroute": "OMNIROUTE_DEFAULT_MODEL",
        }
        attr = model_attrs.get(provider, "DEFAULT_MODEL")
        model = getattr(config, attr, None)
        if isinstance(model, str) and model.strip():
            return model.strip()
        if provider in self._VISION_PROVIDERS:
            return "Provider default / auto-detect at execution"
        return "Not available from current provider settings"

    def _refresh_vision_model(self, *_args):
        """Re-render the model list for the selected provider: the item-0
        provider-default label plus that provider's last successful live
        discovery. The owner's explicit override (typed or selected) is
        captured first and restored afterwards -- a provider change or a
        discovery result never silently replaces it, and an override the
        provider did not list stays selectable."""
        provider = self.vision_provider_combo.currentText().strip()
        override = self._vision_model_override()
        combo = self.vision_model_combo
        combo.blockSignals(True)
        try:
            while combo.count() > 1:
                combo.removeItem(1)
            for model_id in self._vision_discovered_models.get(provider, ()):
                combo.addItem(model_id, model_id)
            combo.setItemText(0, f"Provider default — {self._configured_vision_model(provider)}")
            if override:
                index = combo.findData(override)
                if index < 1:
                    combo.addItem(override, override)
                    index = combo.count() - 1
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)

    def _refresh_vision_models(self):
        """⟳ -- live model discovery for the specialist provider through that
        provider backend's own discover_models(), the same primitive General
        Settings' ⟳ uses; there is no second per-provider implementation and
        no static catalog presented as provider results. Credentials are the
        provider's saved General Settings ones -- the same the routed
        specialist call uses at execution. Persists nothing."""
        from core.backends.base import ModelDiscoveryOutcome, ModelDiscoveryResult

        provider = self.vision_provider_combo.currentText().strip()
        if not provider:
            self.status_lbl.setText("Choose a specialist provider before refreshing models.")
            return
        try:
            from core.backends.loader import get_llm_backend
            result = get_llm_backend(name=provider).discover_models()
        except Exception as exc:
            result = ModelDiscoveryResult(
                ModelDiscoveryOutcome.FAILED,
                diagnostic=f"Model discovery could not start ({type(exc).__name__}).",
            )
        if result.outcome is ModelDiscoveryOutcome.SUCCESS:
            self._vision_discovered_models[provider] = tuple(result.models)
            self._refresh_vision_model()
        self.status_lbl.setText(result.diagnostic)

    def _vision_model_override(self) -> str:
        """The owner's typed/selected capability-specific model override for
        vision_understanding, or "" for "Provider default" -- never a
        display label.

        VISION-RESTORE-01: the visible edit text is the authority, not
        currentIndex()/currentData(). An editable QComboBox keeps its
        previous currentIndex while the owner types (live-verified on
        PySide6 6.11: typed text with or without Enter never drops the
        index to -1), so reading currentData() persisted the PREVIOUS
        selection -- "" (provider default) or a stale override -- and
        silently discarded what was typed. Every non-sentinel item's text
        is its model id, so the text alone is unambiguous; empty text or
        the item-0 label means provider default."""
        combo = self.vision_model_combo
        text = combo.currentText().strip()
        if not text or text == combo.itemText(0).strip():
            return ""
        return text

    def _sync_vision_controls(self, *_args):
        enabled = self.vision_mode_combo.currentData() != "disabled"
        self.vision_provider_combo.setEnabled(enabled)
        self.vision_fallbacks.setEnabled(enabled)
        self.vision_model_combo.setEnabled(enabled)
        self._refresh_vision_model()

    def _sync_image_controls(self, *_args):
        enabled = self.image_mode_combo.currentData() != "disabled"
        self.image_provider_combo.setEnabled(enabled)
        self.image_model_combo.setEnabled(enabled)

    # ── Higgsfield credentials ──
    # Deliberately its own Save action, independent of the tab-wide "Save
    # Settings" button below (which persists TTS/STT/vision-route state to
    # prefs.json/config). Keeping it separate means opening or saving those
    # unrelated fields can never read, write, or clear these two secrets --
    # that code path simply never references them.

    def _refresh_higgsfield_status(self):
        """Truthful Configured/Not configured status, read fresh from the
        secrets store every time -- never cached across a save, so a failed
        or partial write can't leave a stale 'Configured' on screen."""
        has_id = bool(get_secret_safe("higgsfield_key_id"))
        has_secret = bool(get_secret_safe("higgsfield_key_secret"))
        self.hf_status_lbl.setText(
            "API credentials: Configured" if (has_id and has_secret)
            else "API credentials: Not configured"
        )
        self.hf_key_id.setPlaceholderText("•••• configured" if has_id else "Not set")
        self.hf_key_secret.setPlaceholderText("•••• configured" if has_secret else "Not set")

    def _save_higgsfield_credentials(self):
        key_id = self.hf_key_id.text().strip()
        key_secret = self.hf_key_secret.text().strip()
        if not key_id and not key_secret:
            # An untouched (empty) field must never be treated as "clear
            # this credential" -- with nothing typed in either field there
            # is nothing to do, and any existing credential is left exactly
            # as it was.
            self._hf_feedback.success("No credentials entered")
            self.hf_save_status_lbl.setText("")
            return
        from core.secrets import set_secret, set_secrets
        try:
            if key_id and key_secret:
                # Both typed together: one atomic write so the pair can
                # never land half-written (see core.secrets.set_secrets()).
                set_secrets({
                    "higgsfield_key_id": key_id,
                    "higgsfield_key_secret": key_secret,
                })
            elif key_id:
                set_secret("higgsfield_key_id", key_id)
            else:
                set_secret("higgsfield_key_secret", key_secret)
        except Exception as e:
            self._hf_feedback.failure("✗ Failed")
            # Never str(e) -- see safe_error_detail()'s docstring; the
            # exception body is untrusted and must never be able to carry a
            # credential value into a visible label.
            self.hf_save_status_lbl.setText(
                f"Higgsfield credentials were not stored (credential store error: {safe_error_detail(e)})."
            )
            self._refresh_higgsfield_status()
            return
        self.hf_key_id.clear()
        self.hf_key_secret.clear()
        self.hf_save_status_lbl.setText("")
        self._refresh_higgsfield_status()
        self._hf_feedback.success("✓ Saved")

    def _vision_settings_payload(self) -> tuple[dict, list[str]]:
        mode = self.vision_mode_combo.currentData()
        provider = self.vision_provider_combo.currentText().strip()
        if mode == "specialist" and not provider:
            raise ValueError("Specialist route requires a provider.")

        route = dict(self._vision_route_template)
        route["mode"] = mode
        if provider:
            route["specialist"] = provider
        else:
            route.pop("specialist", None)
        route["fallbacks"] = self._csv_values(self.vision_fallbacks.text())
        model_override = self._vision_model_override()
        if model_override:
            route["model"] = model_override
        else:
            route.pop("model", None)

        raw_routes = getattr(config, "MULTIMODAL_ROUTES", {})
        routes = dict(raw_routes) if isinstance(raw_routes, dict) else {}
        if (
            not self._vision_route_present
            and mode == "disabled"
            and not provider
            and not route["fallbacks"]
            and not model_override
        ):
            # Preserve M1's canonical unconfigured/default-disabled state.
            # Saving unrelated speech settings must not manufacture an
            # explicit route that the owner never configured.
            routes.pop("vision_understanding", None)
        else:
            routes["vision_understanding"] = route
        disabled = self._csv_values(self.vision_disabled_providers.text())
        return routes, disabled

    def _image_generation_settings_payload(self) -> Optional[dict]:
        """The persisted image_generation route, or ``None`` when it must
        stay absent.

        Absence-preservation law (mirrors vision_understanding's own
        ``_vision_route_present`` contract exactly): opening Multimodal
        Settings and saving an UNRELATED field must never silently admit a
        new, potentially cost-bearing capability route. A never-configured
        Route mode of "Disabled" therefore stays absent from
        ``multimodal_routes`` entirely -- not merely persisted with
        ``mode: "disabled"`` -- so a caller reading raw prefs sees exactly
        the same "unconfigured" shape as before this campaign. Once the
        route IS present (owner previously enabled it, or enables it this
        save), an explicit disable is recorded truthfully rather than
        erased, exactly like vision's own rule.

        Canonical identifiers only (never the friendly Model label)."""
        mode = self.image_mode_combo.currentData()
        provider = self.image_provider_combo.currentData() or self.image_provider_combo.currentText().strip()
        model = self.image_model_combo.currentData() or self.image_model_combo.currentText().strip()

        if not self._image_route_present and mode == "disabled":
            return None

        route = dict(self._image_route_template)
        route["mode"] = mode
        if mode == "disabled":
            route.pop("specialist", None)
            route.pop("model", None)
        else:
            route["specialist"] = provider
            route["model"] = model
        return route

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
        if not config.TTS_ENABLED or not self.enabled_cb.isChecked():
            self.status_lbl.setText("Enable and save TTS before testing the backend.")
            self._sync_tts_test_enabled()
            return
        if not self._try_enter_busy():
            return
        self.test_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.test_btn.setText("Testing…")
        self.status_lbl.setText("Testing…")
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
                self._apply_live_backend_config(bridge)
                self._attach_backend(bridge)
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
        self.test_btn.setText("✓ Tested" if ok else "✗ Failed")
        self._schedule_feedback_reset(lambda: self.test_btn.setText("▶ Test TTS"))
        self._leave_busy()

    def _save(self):
        try:
            multimodal_routes, disabled_providers = self._vision_settings_payload()
        except ValueError as exc:
            self.status_lbl.setText(str(exc))
            return
        multimodal_routes = dict(multimodal_routes)
        image_route = self._image_generation_settings_payload()
        if image_route is None:
            multimodal_routes.pop("image_generation", None)
        else:
            multimodal_routes["image_generation"] = image_route
        if not self._try_enter_busy():
            return
        self.test_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.save_btn.setText("Saving…")
        self.status_lbl.setText("Saving…")

        was_tts_enabled = bool(getattr(config, "TTS_ENABLED", True))
        previous_tts_backend = getattr(config, "TTS_BACKEND", "kokoro")
        config.TTS_ENABLED = self.enabled_cb.isChecked()
        config.TTS_BACKEND = self.tts_backend_combo.currentText()
        # ElevenLabs is cloud-only -- the URL field doesn't apply to it and
        # must not stomp TTS_HOST with an empty string, same corruption
        # class VOICEBOX_HOST already guards against elsewhere in this tab.
        if config.TTS_BACKEND == "voicebox":
            config.VOICEBOX_HOST = self.url.text().strip()
        elif config.TTS_BACKEND != "elevenlabs":
            config.TTS_HOST = self.url.text().strip()
        # Tracked so the failure message below can be truthful: this write
        # lands in a separate durable file (core/secrets.py, deliberately
        # outside prefs.json) and happens unconditionally before
        # persistence.save() is even attempted, so it survives a prefs
        # write failure regardless of the outcome below.
        credential_written = False
        if config.TTS_BACKEND == "elevenlabs":
            config.ELEVENLABS_API_KEY = self.eleven_key.text().strip()
            from core import secrets as _secrets
            _secrets.set_secret("elevenlabs_api_key", config.ELEVENLABS_API_KEY)
            credential_written = True
        config.STT_ENABLED = self.stt_enabled_cb.isChecked()
        config.STT_BACKEND = self.stt_backend_combo.currentText()
        config.STT_MODEL = self.stt_model_combo.currentText()
        config.STT_DEVICE = self.stt_device_combo.currentText()
        config.MULTIMODAL_ROUTES = multimodal_routes
        config.MULTIMODAL_DISABLED_PROVIDERS = disabled_providers

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
        prefs["multimodal_routes"] = config.MULTIMODAL_ROUTES
        prefs["multimodal_disabled_providers"] = config.MULTIMODAL_DISABLED_PROVIDERS

        # persistence.save() reports failure via its return value, not an
        # exception (see core/persistence.py) -- it was previously called
        # here and ignored, so a failed write looked identical to a
        # successful one. Case A: no backend worker starts, since there's
        # nothing to swap towards. But by this point config.TTS_*/STT_* are
        # already live in-process (they were assigned above, unconditionally,
        # before persistence.save() was ever reached) and -- if ElevenLabs
        # was selected -- the API key is already durably written to
        # credentials.json regardless of what happens to prefs.json. "Save
        # failed" alone would falsely imply nothing changed; only prefs.json
        # itself failed to write, so those live/credential changes will not
        # survive a restart even though they're in effect right now.
        if not persistence.save(prefs):
            fail_msg = "Settings were not fully saved; live values may remain changed until restart."
            if credential_written:
                fail_msg = ("Settings were not fully saved; your ElevenLabs API key was stored, "
                             "and other live values may remain changed until restart.")
            self.save_btn.setText("Save Settings")
            self.status_lbl.setText(fail_msg)
            self._leave_busy()
            return

        if not config.TTS_ENABLED:
            # Detach first: future response/replay speech is suppressed before
            # any potentially blocking backend cleanup begins.
            old_backend = self.agent.tts
            self.agent.tts = None

            def disable_worker():
                try:
                    from tts.loader import unload_tts_backend
                    unload_tts_backend(old_backend)
                    self._tts_swap_done.emit(True, "Settings saved; TTS disabled.")
                except Exception as e:
                    self._tts_swap_done.emit(False, f"Settings saved; TTS cleanup failed: {e}")

            self.status_lbl.setText("Settings saved (finishing TTS shutdown...)")
            threading.Thread(target=disable_worker, daemon=True).start()
            return

        backend_changed = previous_tts_backend != config.TTS_BACKEND
        needs_backend = self.agent.tts is None
        if needs_backend or backend_changed:
            if backend_changed:
                # The loader will dispose the old singleton during reload; do
                # not leave the agent pointing at that detached instance.
                self.agent.tts = None

            def enable_worker():
                # Settings are already persisted at this point -- a failure
                # here is a hot-swap failure, not a "nothing was saved"
                # failure (case B).
                try:
                    from tts.loader import get_tts_backend
                    bridge = get_tts_backend(force_reload=backend_changed)
                    self._apply_live_backend_config(bridge)
                    self._attach_backend(bridge)
                    self._tts_swap_done.emit(True, "Settings saved; TTS enabled.")
                except Exception as e:
                    self._tts_swap_done.emit(False, f"Settings saved; TTS backend swap failed: {e}")

            action = "load" if not was_tts_enabled or needs_backend else "swap"
            self.status_lbl.setText(f"Settings saved (finishing TTS backend {action}...)")
            threading.Thread(target=enable_worker, daemon=True).start()
        else:
            self._apply_live_backend_config(self.agent.tts)
            self.save_btn.setText("✓ Saved")
            self.status_lbl.setText("Settings saved.")
            self._schedule_feedback_reset(lambda: self.save_btn.setText("Save Settings"))
            self._leave_busy()

    def _on_tts_swap_done(self, ok: bool, message: str):
        self.status_lbl.setText(message)
        self.save_btn.setText("✓ Saved" if ok else "⚠ Swap Failed")
        self._schedule_feedback_reset(lambda: self.save_btn.setText("Save Settings"))
        self._leave_busy()


def get_secret_safe(key: str):
    """Local import wrapper so this file doesn't need a hard top-level
    dependency on core.secrets just to check "is something configured"
    (same convention as communications_tab.py's own helper of the same
    name)."""
    try:
        from core.secrets import get_secret
        return get_secret(key)
    except Exception:
        return None


# Compatibility for existing imports and focused TTS/STT behavior tests.  The
# widget itself is now the promoted Multimodal surface; no duplicate page is
# constructed.
TTSTab = MultimodalTab
