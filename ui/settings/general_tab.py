from typing import Optional

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, QLineEdit, QLabel, QCompleter,
)

from PySide6.QtCore import Signal

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence
from core import reasoning_preferences

from ._widgets import _sec, _lbl, _te, _le, _btn, _spin, _combo, _scroll_wrap, ButtonFeedback, safe_error_detail


# ── Tab: General ───────────────────────────────────────────────────────────────

class GeneralTab(QWidget):
    # UI-TRUST-01B: emitted whenever this tab's Save path has live-applied
    # (or attempted to live-apply) a backend/model change -- i.e. whenever
    # the previously displayed connection status may have gone stale.
    # Carries no payload; consumers re-read truth from the live agent.
    backend_connection_changed = Signal()
    CLOUD_BACKENDS = {"openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen"}

    # Patch 3A.4 Part 5 -- raw provider value -> display label. Case-
    # sensitive on the raw value; None is Lumina's own Provider Default
    # sentinel, never a provider-native string. "default" (Groq's literal
    # Qwen-on-Groq effort) is deliberately labeled so it can never render
    # identically to -- or be confused with -- the "Provider Default" item;
    # that collision is exactly the Part 4 Groq/Qwen distinction this whole
    # patch has protected since it was introduced. Any raw value not in
    # this table (an unrecognized future provider string) is handled by
    # _reasoning_label() below, never by extending this dict speculatively.
    _REASONING_LABELS = {
        None: "Provider Default",
        "none": "None",
        "low": "Low",
        "medium": "Medium",
        "high": "High",
        "xhigh": "XHigh",
        "max": "Max",
        "enabled": "Enabled",
        "disabled": "Disabled",
        "default": "Default (Provider Value)",
    }

    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        # Patch 3A.4 Part 5 -- in-session reasoning-effort draft overlay,
        # keyed by (backend_name, model_id) -> raw value (None or a
        # provider-native string). Written ONLY by _on_reasoning_activated()
        # below (a genuine user pick), never by programmatic repopulation.
        # Not written to prefs.json until _save() folds it in via
        # set_saved_reasoning() -- see _save()'s new block below.
        self._pending_reasoning: dict = {}
        # Retained per-backend capability probe instances, keyed by
        # backend name -- see _get_or_create_reasoning_probe() below. Must
        # exist before _build() runs, since _build() populates the
        # reasoning row immediately.
        self._reasoning_probes: dict = {}
        # Successful provider results only, keyed by backend. This keeps a
        # failed refresh useful without ever carrying provider A's models
        # into provider B's UI.
        self._discovered_models: dict[str, tuple[str, ...]] = {}
        self._build()

    def _wlbl(self, text: str) -> QLabel:
        """Word-wrapping label for descriptive copy, not field labels."""
        lbl = _lbl(text, self.c)
        lbl.setWordWrap(True)
        return lbl

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)
        layout.addWidget(_sec("LLM BACKEND", self.c))
        backend_row = QHBoxLayout()
        backend_row.setSpacing(12)
        be_col = QVBoxLayout()
        be_col.addWidget(_lbl("Backend", self.c))
        self.backend_combo = _combo(self.c)
        self.backend_combo.addItems(["llamacpp", "lmstudio", "ollama", "vllm", "openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen", "custom", "omniroute"])
        self.backend_combo.setCurrentText(config.LLM_BACKEND)
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        be_col.addWidget(self.backend_combo)
        url_col = QVBoxLayout()
        url_col.addWidget(_lbl("Server URL", self.c))
        self.url = _le("", self.c)
        url_col.addWidget(self.url)
        backend_row.addLayout(be_col, 1)
        backend_row.addLayout(url_col, 3)
        layout.addLayout(backend_row)

        # ── Model Name / API Key row — shared between "custom" and
        # "omniroute", since both are freeform OpenAI-compatible endpoints.
        # Kept as two independently-configured backend slots (own model,
        # own key) — this widget just repopulates from whichever one is
        # currently selected, in _on_backend_changed() below, so choosing
        # one never clobbers the other's saved values.
        self.custom_model_widget = QWidget()
        cm_layout = QHBoxLayout(self.custom_model_widget)
        cm_layout.setContentsMargins(0, 4, 0, 0)
        cm_layout.setSpacing(12)
        cm_col = QVBoxLayout()
        cm_col.addWidget(_lbl("Model Name", self.c))
        _initial_model = (config.OMNIROUTE_DEFAULT_MODEL if config.LLM_BACKEND == "omniroute"
                          else getattr(config, "CUSTOM_DEFAULT_MODEL", ""))
        self.custom_model = _le(_initial_model, self.c)
        self.custom_model.setPlaceholderText("e.g. mistral-7b-instruct")
        custom_model_row = QHBoxLayout()
        custom_model_row.setSpacing(6)
        custom_model_row.addWidget(self.custom_model, 1)
        self.custom_refresh_models_btn = _btn("⟳", self.c)
        self.custom_refresh_models_btn.setFixedWidth(36)
        self.custom_refresh_models_btn.setToolTip(
            "Fetch models from this server without saving the endpoint or API key"
        )
        self.custom_refresh_models_btn.clicked.connect(self._refresh_models)
        custom_model_row.addWidget(self.custom_refresh_models_btn)
        cm_col.addLayout(custom_model_row)
        cm_layout.addLayout(cm_col, 2)
        cm_key_col = QVBoxLayout()
        cm_key_col.addWidget(_lbl("API Key (optional)", self.c))
        _initial_key = (config.OMNIROUTE_API_KEY if config.LLM_BACKEND == "omniroute"
                        else getattr(config, "CUSTOM_API_KEY", ""))
        self.custom_api_key = _le(_initial_key, self.c)
        self.custom_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.custom_api_key.setPlaceholderText("Bearer token or leave blank")
        cm_key_col.addWidget(self.custom_api_key)
        cm_layout.addLayout(cm_key_col, 2)
        layout.addWidget(self.custom_model_widget)

        # ── Cloud credentials row (hidden for local backends) ──
        self.cloud_widget = QWidget()
        cloud_layout = QHBoxLayout(self.cloud_widget)
        cloud_layout.setContentsMargins(0, 4, 0, 0)
        cloud_layout.setSpacing(12)
        key_col = QVBoxLayout()
        key_col.addWidget(_lbl("API Key", self.c))
        self.cloud_key = _le("", self.c)
        self.cloud_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.cloud_key.setPlaceholderText("sk-...")
        key_col.addWidget(self.cloud_key)
        model_col = QVBoxLayout()
        model_col.addWidget(_lbl("Model", self.c))
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        self.cloud_model = _combo(self.c)
        self.cloud_model.setEditable(True)
        model_row.addWidget(self.cloud_model, 1)
        self.refresh_models_btn = _btn("⟳", self.c)
        self.refresh_models_btn.setFixedWidth(36)
        self.refresh_models_btn.setToolTip("Fetch available models from this backend using the API key above")
        self.refresh_models_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(self.refresh_models_btn)
        model_col.addLayout(model_row)
        cloud_layout.addLayout(key_col, 2)
        cloud_layout.addLayout(model_col, 2)
        layout.addWidget(self.cloud_widget)
        self._refresh_cloud_row(config.LLM_BACKEND)  # set initial state
        self._refresh_endpoint_row(config.LLM_BACKEND)

        # ── Reasoning Effort (Patch 3A.4 Part 5) ──
        # Always visible regardless of backend -- unlike cloud_widget/
        # custom_model_widget above, a backend/model with no selectable
        # reasoning control still gets this row, just disabled at Provider
        # Default (see _refresh_reasoning_row()'s state machine below).
        # Deliberately its own section, structurally distinct from
        # THINKING DISPLAY further down: that checkbox only controls
        # whether think-blocks are RENDERED in chat; this row controls how
        # much the model reasons in the first place -- two independent
        # facts, never merged into one control.
        layout.addWidget(_sec("REASONING EFFORT", self.c))
        layout.addWidget(self._wlbl(
            "How much some models reason before answering, when the "
            "backend/model exposes a selectable control. Remembered per "
            "backend and model, independently of everything else on this "
            "tab -- switching backends or models above recalls each one's "
            "own saved choice."
        ))
        reasoning_row = QHBoxLayout()
        reasoning_col = QVBoxLayout()
        reasoning_col.addWidget(_lbl("Effort", self.c))
        self.reasoning_combo = _combo(self.c)
        # `activated` -- not currentIndexChanged/currentTextChanged -- is
        # the ONLY signal wired to recording a pending edit: it fires only
        # on genuine user interaction with the popup (including re-picking
        # the already-current item), and never fires from the programmatic
        # blockSignals()-guarded repopulation in _populate_reasoning_combo()
        # below. See _on_reasoning_activated()'s docstring for why this
        # distinction matters.
        self.reasoning_combo.activated.connect(self._on_reasoning_activated)
        reasoning_col.addWidget(self.reasoning_combo)
        reasoning_row.addLayout(reasoning_col, 1)
        reasoning_row.addStretch(2)
        layout.addLayout(reasoning_row)
        # Both editable model fields recall this backend's saved/pending
        # reasoning choice the instant the model text changes -- no Save
        # needed to see e.g. Sol's "Max" reappear after switching away and
        # back to it within the same session.
        self.cloud_model.currentTextChanged.connect(self._on_reasoning_model_changed)
        self.custom_model.textChanged.connect(self._on_reasoning_model_changed)
        self._refresh_reasoning_row()  # set initial state

        layout.addWidget(_sec("CONTEXT WINDOW", self.c))
        layout.addWidget(self._wlbl(
            "Max Context Tokens, Memory Inject Limit, and Tool Result Max Chars "
            "are saved per-backend — switching backends above recalls that "
            "backend's own values."
        ))
        row1 = QHBoxLayout()
        row1.setSpacing(16)
        ctx_col = QVBoxLayout()
        ctx_col.addWidget(_lbl("Max Context Tokens", self.c))
        self.ctx_spin = _spin(config.MAX_CONTEXT_TOKENS, 1024, 1048576, 1024, self.c)
        ctx_col.addWidget(self.ctx_spin)
        mem_col = QVBoxLayout()
        mem_col.addWidget(_lbl("Memory Inject Limit", self.c))
        self.mem_spin = _spin(config.MEMORY_INJECT_LIMIT, 1, 200, 1, self.c)
        mem_col.addWidget(self.mem_spin)
        result_col = QVBoxLayout()
        result_col.addWidget(_lbl("Tool Result Max Chars", self.c))
        self.result_spin = _spin(config.TOOL_RESULT_MAX_CHARS, 500, 500000, 500, self.c)
        result_col.addWidget(self.result_spin)
        row1.addLayout(ctx_col)
        row1.addLayout(mem_col)
        row1.addLayout(result_col)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(16)
        iter_col = QVBoxLayout()
        iter_col.addWidget(_lbl("Max Tool Iterations", self.c))
        self.iter_spin = _spin(config.MAX_TOOL_ITERATIONS, 1, 100, 1, self.c)
        iter_col.addWidget(self.iter_spin)
        resp_col = QVBoxLayout()
        resp_col.addWidget(_lbl("Response Tokens", self.c))
        self.resp_spin = _spin(config.RESPONSE_RESERVE_TOKENS, 256, 1048576, 256, self.c)
        resp_col.addWidget(self.resp_spin)
        row2.addLayout(iter_col)
        row2.addLayout(resp_col)
        layout.addLayout(row2)

        row_compaction = QHBoxLayout()
        row_compaction.setSpacing(16)
        self.compaction_enabled_cb = QCheckBox("Enable Context-Trim Compaction")
        self.compaction_enabled_cb.setChecked(config.CONTEXT_COMPACTION_ENABLED)
        self.compaction_enabled_cb.setToolTip(
            "When history is trimmed to fit the context budget, summarize what "
            "was dropped instead of discarding it, and store it in memory."
        )
        self.compaction_enabled_cb.toggled.connect(self._on_compaction_toggled)
        compaction_batch_col = QVBoxLayout()
        compaction_batch_col.addWidget(_lbl("Compaction Batch Tokens", self.c))
        self.compaction_batch_spin = _spin(config.CONTEXT_COMPACTION_BATCH_TOKENS, 100, 50000, 100, self.c)
        compaction_batch_col.addWidget(self.compaction_batch_spin)
        row_compaction.addWidget(self.compaction_enabled_cb)
        row_compaction.addLayout(compaction_batch_col)
        layout.addLayout(row_compaction)
        self._on_compaction_toggled(config.CONTEXT_COMPACTION_ENABLED)  # set initial enabled state

        # ── Dreaming ──
        layout.addWidget(_sec("DREAMING", self.c))
        layout.addWidget(self._wlbl(
            "Idle-sweep background summarization — writes brief session recaps "
            "into memory after a chat sits idle. Was previously only "
            "configurable by editing config.py directly."
        ))
        row3 = QHBoxLayout()
        row3.setSpacing(16)
        self.dream_enabled_cb = QCheckBox("Enable Dream Sweeps")
        self.dream_enabled_cb.setChecked(config.DREAM_SWEEP_ENABLED)
        self.dream_enabled_cb.toggled.connect(self._on_dream_toggled)
        idle_col = QVBoxLayout()
        idle_col.addWidget(_lbl("Idle Minutes Before Sweep", self.c))
        self.dream_idle_spin = _spin(config.DREAM_IDLE_MINUTES, 1, 180, 1, self.c)
        idle_col.addWidget(self.dream_idle_spin)
        row3.addWidget(self.dream_enabled_cb)
        row3.addLayout(idle_col)
        layout.addLayout(row3)
        self._on_dream_toggled(config.DREAM_SWEEP_ENABLED)  # set initial enabled state

        # ── Thinking display ──
        layout.addWidget(_sec("THINKING DISPLAY", self.c))
        self.show_think_cb = QCheckBox("Show reasoning (think blocks) in chat")
        self.show_think_cb.setChecked(config.SHOW_THINK_BLOCKS)
        self.show_think_cb.setToolTip(
            "Display-only — the model still reasons either way; this just "
            "controls whether that reasoning is rendered in the chat window."
        )
        layout.addWidget(self.show_think_cb)

        layout.addWidget(_sec("GLOBAL AGENT BEHAVIOR PROMPT", self.c))
        layout.addWidget(_lbl("Global agentic system prompt that works in conjunction with all Persona prompts.", self.c))
        self.prompt = _te(config.SYSTEM_PROMPT, self.c, height=140)
        layout.addWidget(self.prompt)
        btn_row = QHBoxLayout()
        self.apply_btn = _btn("Apply Change", self.c)
        self.apply_btn.clicked.connect(self._on_apply_clicked)
        self.save_btn = _btn("Save All Settings", self.c, accent=True)
        self.save_btn.clicked.connect(self._save)
        btn_row.addWidget(self.apply_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.save_btn)
        layout.addLayout(btn_row)
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color:{self.c['text_muted']};font-size:11px;background:transparent;")
        layout.addWidget(self.status_lbl)
        self._apply_feedback = ButtonFeedback(self.apply_btn)
        self._save_feedback = ButtonFeedback(self.save_btn)
        layout.addStretch()
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    def _on_dream_toggled(self, checked: bool):
        self.dream_idle_spin.setEnabled(checked)

    def _on_compaction_toggled(self, checked: bool):
        self.compaction_batch_spin.setEnabled(checked)

    def _refresh_cloud_row(self, backend: str):
        is_cloud = backend in self.CLOUD_BACKENDS
        self.cloud_widget.setVisible(is_cloud)
        if is_cloud:
            key_attr = f"{backend.upper()}_API_KEY"
            model_attr = f"{backend.upper()}_DEFAULT_MODEL"
            self.cloud_key.setText(getattr(config, key_attr, ""))
            current = getattr(config, model_attr, "")
            self.cloud_model.clear()
            self.cloud_model.addItems(list(self._discovered_models.get(backend, ())))
            self.cloud_model.setCurrentText(current)

    def _refresh_freeform_model_suggestions(self, backend: str):
        """Attach only this backend's last successful IDs as completions."""
        models = list(self._discovered_models.get(backend, ()))
        self.custom_model.setCompleter(
            QCompleter(models, self.custom_model) if models else None
        )
    def _apply_prompt(self) -> bool:
        """Pure action, no feedback -- called both by the Apply Change click
        handler and (unconditionally, when the field is non-empty) from
        _save()'s tail. Feedback belongs to each of those callers, not
        here, or Save would also flash Apply's button. Returns True if the
        live prompt actually changed, False for the empty-field no-op."""
        p = self.prompt.toPlainText().strip()
        if not p:
            return False
        persona = getattr(self.agent, "current_persona", None)
        if persona and "system_prompt" in persona:
            # Recombine live: new global rules + whatever persona identity
            # is actually active, same order apply_persona() uses. Without
            # this, "Apply Change" would silently discard the persona's
            # identity text entirely -- a flat replace, not a preview.
            new_prompt = p + "\n\n" + persona["system_prompt"]
            self.agent.ctx.update_system_prompt(new_prompt)
        else:
            self.agent.ctx.update_system_prompt(p)
        return True

    def _on_apply_clicked(self):
        try:
            applied = self._apply_prompt()
        except Exception as e:
            self._apply_feedback.failure("✗ Failed")
            self.status_lbl.setText(f"Apply failed: {e}")
            return
        if applied:
            self._apply_feedback.success("✓ Applied")
        else:
            self._apply_feedback.success("No change")
        self.status_lbl.setText("")

    def _refresh_endpoint_row(self, backend: str):
        """Render endpoint truth from the backend contract, not UI tables."""
        from core.backends.loader import endpoint_is_configurable, get_backend_endpoint

        configurable = endpoint_is_configurable(backend)
        self.url.setText(get_backend_endpoint(backend))
        self.url.setEnabled(True)
        self.url.setReadOnly(not configurable)
        self.url.setPlaceholderText(
            "Enter your OpenAI-compatible endpoint URL" if configurable else ""
        )

    def _on_backend_changed(self, name: str):
        self._refresh_cloud_row(name)
        self._refresh_endpoint_row(name)
        is_freeform = name in ("custom", "omniroute")
        self.custom_model_widget.setVisible(is_freeform)
        if name == "omniroute":
            self.custom_model.setText(config.OMNIROUTE_DEFAULT_MODEL)
            self.custom_api_key.setText(config.OMNIROUTE_API_KEY)
            self.custom_model.setPlaceholderText("e.g. kr/glm-5, if/kimi-k2-thinking")
        elif name == "custom":
            self.custom_model.setText(getattr(config, "CUSTOM_DEFAULT_MODEL", ""))
            self.custom_api_key.setText(getattr(config, "CUSTOM_API_KEY", ""))
            self.custom_model.setPlaceholderText("e.g. mistral-7b-instruct")
        if is_freeform:
            self._refresh_freeform_model_suggestions(name)
        self._refresh_context_row(name)
        # Patch 3A.4 Part 5 -- after the model field for the new backend
        # has been populated above, so this reads the right model text.
        self._refresh_reasoning_row()

    def _refresh_context_row(self, backend: str):
        """Recall this backend's saved Max Context Tokens / Memory Inject
        Limit / Tool Result Max Chars, or fall back to
        config.BACKEND_CONTEXT_DEFAULTS if this backend has never been saved
        before. Only these three are per-backend — Max Tool Iterations and
        Response Tokens stay global (set once below, not touched here)."""
        prefs = persistence.load()
        saved = prefs.get("backend_context", {}).get(backend, {})
        default = config.BACKEND_CONTEXT_DEFAULTS.get(backend, config.BACKEND_CONTEXT_DEFAULTS["llamacpp"])
        self.ctx_spin.setValue(saved.get("max_context_tokens", default["max_context_tokens"]))
        self.mem_spin.setValue(saved.get("memory_inject_limit", default["memory_inject_limit"]))
        self.result_spin.setValue(saved.get("tool_result_max_chars", default["tool_result_max_chars"]))

    # ------------------------------------------------------------------
    # Patch 3A.4 Part 5 -- Reasoning Effort row
    # ------------------------------------------------------------------

    def _current_reasoning_model(self) -> Optional[str]:
        """
        The exact same model-identity string _save() will persist for the
        currently-selected backend -- cloud backends read cloud_model's
        current text, custom/omniroute read custom_model's text. No
        normalization beyond stripping accidental surrounding whitespace,
        matching _save()'s own .strip() calls exactly -- a pending entry
        recorded under a differently-derived string would silently target
        the wrong dict key.

        llamacpp/lmstudio/ollama/vllm have no model field on this tab at
        all (only Server URL) -- deliberately not falling back to
        self.agent.llm.get_model() to find one: get_model() performs a
        live HTTP call for LMStudio/Ollama when no model is configured yet
        (see BaseLLMBackend.reasoning_capabilities()'s docstring), which
        would be a surprising side effect of merely opening or repopulating
        this Settings tab. These backends resolve to None here and get the
        same disabled-at-Provider-Default treatment as any other
        no-capability-data case.
        """
        backend_name = self.backend_combo.currentText()
        if backend_name in self.CLOUD_BACKENDS:
            model = self.cloud_model.currentText().strip()
        elif backend_name in ("custom", "omniroute"):
            model = self.custom_model.text().strip()
        else:
            model = ""
        return model or None

    def _reasoning_label(self, value: Optional[str]) -> str:
        """Raw provider value -> display label. See _REASONING_LABELS for
        the fixed cases; any other raw string (an unrecognized future
        provider value) still gets a reasonable derived label instead of
        crashing or being silently dropped -- the raw item data set by the
        caller is what's semantically stored, never this label."""
        if value in self._REASONING_LABELS:
            return self._REASONING_LABELS[value]
        return str(value).replace("_", " ").title()

    def _on_reasoning_model_changed(self, _text=None):
        self._refresh_reasoning_row()

    def _on_reasoning_activated(self, index: int):
        """
        The ONLY place self._pending_reasoning is ever written. `activated`
        fires exclusively on genuine popup interaction -- including
        re-picking the item that's already current -- unlike
        currentIndexChanged/currentTextChanged, which do NOT fire when the
        effective value doesn't change. That distinction is why this
        signal (and not one of those) is wired to recording a real user
        choice: it's the only one that lets a user deliberately reset a
        stale value back to "Provider Default" even when Provider Default
        is already what's displayed. Programmatic repopulation
        (_populate_reasoning_combo) always runs inside
        blockSignals(True)/(False), so it can never reach this handler.
        """
        backend_name = self.backend_combo.currentText()
        model = self._current_reasoning_model()
        if model is None:
            return
        value = self.reasoning_combo.itemData(index)
        self._pending_reasoning[(backend_name, model)] = value

    def _populate_reasoning_combo(self, efforts, selected: Optional[str],
                                   enabled: bool, tooltip: str):
        """
        Programmatic repopulation -- clear/addItem/setCurrentIndex all run
        under blockSignals(True) so this can never be misread as a user
        edit (see _on_reasoning_activated() above). Item 0 is always
        Provider Default (data=None); `efforts` is appended after it in
        the exact order given -- never re-sorted, never hardcoded here.
        `selected`'s informational default_effort is NEVER substituted in
        as the selection -- Provider Default always means "no override,"
        never "silently pick the provider's own default" -- callers only
        ever pass a real saved/pending value or None.
        """
        self.reasoning_combo.blockSignals(True)
        try:
            self.reasoning_combo.clear()
            self.reasoning_combo.addItem(self._reasoning_label(None), None)
            for effort in efforts:
                self.reasoning_combo.addItem(self._reasoning_label(effort), effort)
            target_index = 0
            if selected is not None:
                idx = self.reasoning_combo.findData(selected)
                if idx >= 0:
                    target_index = idx
            self.reasoning_combo.setCurrentIndex(target_index)
        finally:
            self.reasoning_combo.blockSignals(False)
        self.reasoning_combo.setEnabled(enabled)
        self.reasoning_combo.setToolTip(tooltip)

    def _get_or_create_reasoning_probe(self, backend_name: str):
        """
        Return a backend instance to query reasoning capabilities against
        for `backend_name`, constructing (and retaining) one on first
        need. Reusing a previously retained instance -- rather than
        constructing a fresh one on every repopulation -- is the whole
        point: OpenRouterBackend's capability cache (Part 2B) lives on the
        instance itself, so a fresh construct would silently reset it to
        empty/not-ready every time, defeating both the no-redundant-HTTP
        guarantee and the point of the Refresh Models button. Credentials
        typed in Settings are passed directly to the probe constructor;
        constructing it must never count as a save or create a process-
        global config mutation window.
        """
        probe = self._reasoning_probes.get(backend_name)
        if probe is not None:
            return probe
        from core.backends.loader import get_llm_backend
        credential = None
        if backend_name in self.CLOUD_BACKENDS:
            credential = self.cloud_key.text().strip()
        elif backend_name in ("custom", "omniroute"):
            credential = self.custom_api_key.text().strip()
        try:
            probe = get_llm_backend(
                name=backend_name,
                url=self.url.text().strip(),
                api_key=credential,
            )
        except Exception:
            probe = None
        if probe is not None:
            self._reasoning_probes[backend_name] = probe
        return probe

    def _refresh_reasoning_row(self):
        """
        The Reasoning Effort row's full state machine. Pure consumer of
        whatever reasoning_capabilities()/reasoning_capabilities_ready()/
        refresh_reasoning_capabilities() say for the currently-selected
        backend/model -- no per-backend branching lives here.

        Six cases (see Patch 3A.4 Part 5 brief for the full enumeration):
          1. Static backend/model, efforts non-empty -> enabled, populated.
          2. Static backend/model, efforts empty, not mandatory -> disabled
             at Provider Default, "no selectable control" tooltip.
          3. Static backend/model, efforts empty, mandatory -> disabled at
             Provider Default, "always reasons" tooltip (distinct from 2).
          4. Not yet discovered (OpenRouter pre-refresh) -> disabled at
             Provider Default, "hasn't loaded yet" tooltip -- distinct
             from 2/6, never claims non-support as a known fact.
          5. Discovered, efforts non-empty -> behaves like case 1.
          6. Discovered, efforts empty for this model -> behaves like case
             2 -- genuinely known now, distinct tooltip-wise from case 4.
        """
        backend_name = self.backend_combo.currentText()
        model = self._current_reasoning_model()
        key = (backend_name, model)

        if key in self._pending_reasoning:
            value = self._pending_reasoning[key]
        elif model is not None:
            prefs = persistence.load()
            value = reasoning_preferences.get_saved_reasoning(prefs, backend_name, model)
        else:
            value = None

        if model is None:
            self._populate_reasoning_combo(
                efforts=(), selected=None, enabled=False,
                tooltip="No model is configured for this backend yet.",
            )
            return

        # Prefer the live agent backend when it matches and is already
        # ready, to avoid duplicate discovery -- but never mutate it
        # (never call refresh_reasoning_capabilities() on it from this
        # passive path); only fall back to a Settings-owned probe.
        live = self.agent.llm
        if getattr(live, "name", None) == backend_name and live.reasoning_capabilities_ready(model):
            instance = live
        else:
            instance = self._get_or_create_reasoning_probe(backend_name)
        if instance is None:
            self._populate_reasoning_combo(
                efforts=(), selected=None, enabled=False,
                tooltip="Unable to determine this backend's reasoning capabilities.",
            )
            return

        ready = instance.reasoning_capabilities_ready(model)

        if not ready and value is not None:
            # An explicit pending/saved value exists but this instance
            # hasn't discovered capability data yet -- refresh exactly
            # once, always through the Settings-owned probe (never
            # self.agent.llm -- see above). Skipped entirely when `value`
            # is None: there is nothing to validate, so no network call is
            # ever justified merely to prove Provider Default is valid.
            probe = self._get_or_create_reasoning_probe(backend_name)
            refreshed_ok = probe.refresh_reasoning_capabilities()
            instance = probe
            ready = probe.reasoning_capabilities_ready(model)
            if not refreshed_ok:
                # Refresh failed -- Provider Default, disabled, and the
                # underlying pending/saved value is left completely
                # untouched (no write into self._pending_reasoning here).
                self._populate_reasoning_combo(
                    efforts=(), selected=None, enabled=False,
                    tooltip=("Reasoning capability info for this model "
                             "could not be loaded. Your saved selection "
                             "was left unchanged."),
                )
                return

        if not ready:
            # Genuinely not-yet-known (only reachable when value was None
            # above) -- must never be conflated with "known and
            # unsupported" (that's the `caps.efforts` empty branch below).
            self._populate_reasoning_combo(
                efforts=(), selected=None, enabled=False,
                tooltip=("Reasoning capability info for this model hasn't "
                         "been loaded yet. Use Refresh Models to check."),
            )
            return

        caps = instance.reasoning_capabilities(model)

        if not caps.efforts:
            if caps.mandatory:
                tooltip = ("This model always reasons and does not expose "
                           "a selectable effort level.")
            else:
                tooltip = "This backend/model does not expose a selectable reasoning control."
            self._populate_reasoning_combo(efforts=(), selected=None, enabled=False, tooltip=tooltip)
            return

        # caps.default_effort is informational-only -- appended to the
        # tooltip, never substituted in as `selected`.
        selected = caps.validate(value)
        tooltip = "Controls how much this model reasons before answering."
        if caps.default_effort is not None:
            tooltip += f" Provider default: {self._reasoning_label(caps.default_effort)}."
        self._populate_reasoning_combo(efforts=caps.efforts, selected=selected, enabled=True, tooltip=tooltip)

    def _refresh_models(self):
        """Run truthful live discovery without persisting draft settings."""
        backend_name = self.backend_combo.currentText()
        credential = None
        if backend_name in self.CLOUD_BACKENDS:
            credential = self.cloud_key.text().strip()
        elif backend_name in ("custom", "omniroute"):
            credential = self.custom_api_key.text().strip()
        try:
            from core.backends.loader import get_llm_backend
            from core.backends.base import ModelDiscoveryOutcome
            probe = get_llm_backend(
                name=backend_name,
                url=self.url.text().strip(),
                api_key=credential,
            )
            result = probe.discover_models()
        except Exception as exc:
            from core.backends.base import ModelDiscoveryOutcome, ModelDiscoveryResult
            probe = None
            result = ModelDiscoveryResult(
                ModelDiscoveryOutcome.FAILED,
                diagnostic=f"Model discovery could not start ({type(exc).__name__}).",
            )

        # Only a valid response replaces the retained capability probe. A
        # failed refresh must preserve OpenRouter's previous good cache.
        if probe is not None and result.outcome in (
            ModelDiscoveryOutcome.SUCCESS,
            ModelDiscoveryOutcome.EMPTY,
        ):
            self._reasoning_probes[backend_name] = probe

        if result.outcome is ModelDiscoveryOutcome.SUCCESS:
            self._discovered_models[backend_name] = result.models
            if backend_name in self.CLOUD_BACKENDS:
                current = self.cloud_model.currentText()
                self.cloud_model.clear()
                self.cloud_model.addItems(list(result.models))
                if current:
                    self.cloud_model.setCurrentText(current)
            elif backend_name in ("custom", "omniroute"):
                self._refresh_freeform_model_suggestions(backend_name)
            self.status_lbl.setText(result.diagnostic)
        elif result.outcome is ModelDiscoveryOutcome.EMPTY:
            self.status_lbl.setText(result.diagnostic)
        elif result.outcome is ModelDiscoveryOutcome.FAILED:
            suffix = (
                " Offline suggestions are available, but were not shown as provider results."
                if result.offline_suggestions else ""
            )
            self.status_lbl.setText(result.diagnostic + suffix)
        else:
            suffix = (
                " Offline suggestions are available, but live enumeration is unsupported."
                if result.offline_suggestions else ""
            )
            self.status_lbl.setText(result.diagnostic + suffix)

        self._refresh_reasoning_row()

    def _save(self):
        from core.backends.loader import (
            BACKENDS,
            endpoint_is_configurable,
            get_backend_endpoint,
            get_llm_backend,
            migrate_legacy_backend_endpoint,
        )
        # Consume the old universal URL against the backend it originally
        # accompanied before changing the selected backend below.  This is a
        # one-time no-op after migration.
        migrate_legacy_backend_endpoint(persist=False)
        new_system_prompt = self.prompt.toPlainText().strip()
        if new_system_prompt:
            config.SYSTEM_PROMPT = new_system_prompt
        config.MAX_CONTEXT_TOKENS = self.ctx_spin.value()
        config.MEMORY_INJECT_LIMIT = self.mem_spin.value()
        config.TOOL_RESULT_MAX_CHARS = self.result_spin.value()
        config.MAX_TOOL_ITERATIONS = self.iter_spin.value()
        config.RESPONSE_RESERVE_TOKENS = self.resp_spin.value()
        config.CONTEXT_COMPACTION_ENABLED = self.compaction_enabled_cb.isChecked()
        config.CONTEXT_COMPACTION_BATCH_TOKENS = self.compaction_batch_spin.value()
        config.DREAM_SWEEP_ENABLED = self.dream_enabled_cb.isChecked()
        config.DREAM_IDLE_MINUTES = self.dream_idle_spin.value()
        config.SHOW_THINK_BLOCKS = self.show_think_cb.isChecked()
        config.LLM_BACKEND = self.backend_combo.currentText()
        backend_endpoints = {
            name: value
            for name, value in getattr(config, "BACKEND_ENDPOINTS", {}).items()
            if name in BACKENDS
            and BACKENDS[name].endpoint_configurable
            and isinstance(value, str)
        }
        if endpoint_is_configurable(config.LLM_BACKEND):
            backend_endpoints[config.LLM_BACKEND] = self.url.text().strip()
        config.BACKEND_ENDPOINTS = backend_endpoints
        config.BACKEND_ENDPOINTS_MIGRATED = True
        # Compatibility snapshot only.  Backend construction no longer reads
        # this as universal endpoint authority.
        config.LLM_BACKEND_URL = (
            backend_endpoints[config.LLM_BACKEND]
            if endpoint_is_configurable(config.LLM_BACKEND)
            else get_backend_endpoint(config.LLM_BACKEND)
        )
        config.LM_STUDIO_BASE_URL = config.LLM_BACKEND_URL

        # Cloud credentials
        if config.LLM_BACKEND in self.CLOUD_BACKENDS:
            key_attr = f"{config.LLM_BACKEND.upper()}_API_KEY"
            model_attr = f"{config.LLM_BACKEND.upper()}_DEFAULT_MODEL"
            setattr(config, key_attr, self.cloud_key.text().strip())
            setattr(config, model_attr, self.cloud_model.currentText().strip())


        from core.persistence import load as load_prefs, save as save_prefs
        prefs = load_prefs()
        prefs["llm_backend"] = config.LLM_BACKEND
        prefs["llm_backend_url"] = config.LLM_BACKEND_URL
        prefs["backend_endpoints"] = backend_endpoints
        prefs["backend_endpoints_migrated"] = True

        # Context/memory settings — max_tool_iterations and
        # response_reserve_tokens are global; max_context_tokens,
        # memory_inject_limit, and tool_result_max_chars are saved per-backend
        # so switching backends recalls each one's own values instead of
        # clobbering the other.
        prefs["max_tool_iterations"] = config.MAX_TOOL_ITERATIONS
        prefs["response_reserve_tokens"] = config.RESPONSE_RESERVE_TOKENS
        prefs["context_compaction_enabled"] = config.CONTEXT_COMPACTION_ENABLED
        prefs["context_compaction_batch_tokens"] = config.CONTEXT_COMPACTION_BATCH_TOKENS
        prefs["dream_sweep_enabled"] = config.DREAM_SWEEP_ENABLED
        prefs["dream_idle_minutes"] = config.DREAM_IDLE_MINUTES
        prefs["show_think_blocks"] = config.SHOW_THINK_BLOCKS
        prefs["system_prompt"] = config.SYSTEM_PROMPT
        backend_context = prefs.get("backend_context", {})
        backend_context[config.LLM_BACKEND] = {
            "max_context_tokens": config.MAX_CONTEXT_TOKENS,
            "memory_inject_limit": config.MEMORY_INJECT_LIMIT,
            "tool_result_max_chars": config.TOOL_RESULT_MAX_CHARS,
        }
        prefs["backend_context"] = backend_context

        # Patch 3A.4 Part 5 -- fold every in-session reasoning-effort edit
        # (self._pending_reasoning, written ONLY via the reasoning combo's
        # own `activated` signal -- never by passive repopulation) into
        # this same prefs dict, for EVERY pending (backend, model) entry,
        # not just whichever one is currently visible in the row -- e.g.
        # configuring Sonnet 5, Sol, and Luna across one session and
        # having a single Save persist all three. Rides inside this one
        # save_prefs() transaction below -- no separate persistence.save()
        # call -- so a reasoning edit can never persist independently of
        # (or out of sync with) everything else Save already does
        # atomically.
        for (pending_backend, pending_model), pending_value in self._pending_reasoning.items():
            reasoning_preferences.set_saved_reasoning(prefs, pending_backend, pending_model, pending_value)

        # S41 fix: cloud API key/model used to only live in the running
        # config module (setattr above) and revert to "" on restart — same
        # bug class the context settings had. Only write an entry when this
        # is actually a cloud backend; local/custom backends don't have one.
        # FE-09: the key itself now goes to secrets.py, not prefs.json —
        # prefs.json gets dragged into Project uploads and the public repo,
        # secrets.py never does. Only default_model stays in prefs.
        # Credential writes (cloud/custom/omniroute) all happen here, before
        # save_prefs() below -- same order the original code always used.
        # set_secret() raises rather than returning False (see core/secrets.py),
        # and previously an unhandled raise here silently aborted the rest of
        # _save() (no persistence, no backend swap) with zero visible
        # feedback. Catching it preserves that exact same "stops here"
        # behavior -- just with a truthful message instead of a swallowed
        # traceback. config.* above is already mutated live regardless.
        #
        # credential_written is set True only immediately AFTER a set_secret()
        # call actually returns without raising -- not predicted from backend
        # type up front. LLM_BACKEND is a single string, so cloud vs.
        # custom/omniroute are mutually exclusive: at most one set_secret()
        # call ever happens per invocation, never two. But for custom/
        # omniroute, self.agent.llm._model = ... runs in the SAME try block
        # immediately after that call succeeds, and can itself raise (e.g.
        # agent.llm is None) -- a real partial-state case where the credential
        # genuinely IS already durably stored even though this except block
        # still fires. The flag lets the except branch below tell that case
        # apart from "the secret write itself is what failed," instead of
        # always claiming a uniform "nothing was saved."
        credential_written = False
        try:
            if config.LLM_BACKEND in self.CLOUD_BACKENDS:
                from core import secrets as _secrets
                _secrets.set_secret(f"{config.LLM_BACKEND}_api_key", self.cloud_key.text().strip())
                credential_written = True
                cloud_creds = prefs.get("cloud_credentials", {})
                cloud_creds[config.LLM_BACKEND] = {
                    "default_model": self.cloud_model.currentText().strip(),
                }
                prefs["cloud_credentials"] = cloud_creds

            if config.LLM_BACKEND == "custom":
                config.CUSTOM_DEFAULT_MODEL = self.custom_model.text().strip()
                config.CUSTOM_API_KEY = self.custom_api_key.text().strip()
                from core import secrets as _secrets
                _secrets.set_secret("custom_api_key", config.CUSTOM_API_KEY)
                credential_written = True
                prefs["custom_default_model"] = config.CUSTOM_DEFAULT_MODEL
                self.agent.llm._model = config.CUSTOM_DEFAULT_MODEL
            elif config.LLM_BACKEND == "omniroute":
                config.OMNIROUTE_DEFAULT_MODEL = self.custom_model.text().strip()
                config.OMNIROUTE_API_KEY = self.custom_api_key.text().strip()
                from core import secrets as _secrets
                _secrets.set_secret("omniroute_api_key", config.OMNIROUTE_API_KEY)
                credential_written = True
                prefs["omniroute_default_model"] = config.OMNIROUTE_DEFAULT_MODEL
                self.agent.llm._model = config.OMNIROUTE_DEFAULT_MODEL
        except Exception as e:
            self._save_feedback.failure("✗ Failed")
            # Never str(e) here -- see safe_error_detail()'s docstring. The
            # exception body is untrusted.
            error_kind = safe_error_detail(e)
            if credential_written:
                # set_secret() itself already succeeded -- this exception
                # came from a LATER same-try live-apply step (e.g.
                # self.agent.llm._model = ...), not from credential storage.
                # Must not be mislabeled as a credential-store error, and
                # must say the credential really is already durably stored.
                msg = (
                    "Settings were not fully saved; your credential was "
                    f"stored, but a later live update failed ({error_kind})."
                )
            else:
                # The credential write itself is what raised -- nothing was
                # durably stored. Only the earlier live config mutation
                # (not the credential) is known to have already changed.
                msg = (
                    "Settings were not fully saved; some live values may "
                    f"already have changed. Credential storage failed ({error_kind})."
                )
            self.status_lbl.setText(msg)
            return

        # UI-TRUST-01B: custom/omniroute already mutated agent.llm._model
        # inside the credential block above, so the status bar may be stale
        # from this point on -- even if prefs persistence fails below.
        self.backend_connection_changed.emit()

        # persistence.save() reports failure via its return value, not an
        # exception (core/persistence.py) -- it was previously called here
        # and ignored, so a failed write looked identical to a successful
        # one. By this point config.* is already live and, for a cloud/
        # custom/omniroute backend, the credential is already durably
        # written to secrets.py regardless of what happens to prefs.json --
        # mirrors TTSTab._save()'s same distinction (see tts_tab.py).
        if not save_prefs(prefs):
            self._save_feedback.failure("⚠ Not Saved")
            if credential_written:
                msg = ("Settings were not fully saved; your API credentials "
                       "were stored, and other live values may remain "
                       "changed until restart.")
            else:
                msg = "Settings were not fully saved; live values may remain changed until restart."
            self.status_lbl.setText(msg)
            return

        # Prefs (including every pending reasoning-effort edit above) are
        # now durably saved -- clear the in-session draft overlay, since
        # it's already on disk and get_saved_reasoning() will recover it
        # on the next repopulation. Deliberately NOT cleared on either
        # failure branch above (Not Saved / Failed), where these edits are
        # NOT yet durable and must survive for the user's next Save
        # attempt.
        self._pending_reasoning = {}

        # Prefs are durably saved at this point. A failure from here on is a
        # live-apply failure, not a "nothing was saved" failure -- must say
        # so rather than reporting it as an outright Save failure.
        try:
            self.agent.llm = get_llm_backend()

            # Apply context/memory settings to the live agent immediately —
            # no restart needed, matching the backend-swap precedent above.
            # ContextManager captured these at construction time (max_tokens,
            # reserve); update the instance directly, not just the config
            # module, or a hot-reload gap opens up (see F-09: from-import
            # snapshotting).
            self.agent.ctx.max_tokens = config.MAX_CONTEXT_TOKENS
            self.agent.ctx.reserve = config.RESPONSE_RESERVE_TOKENS
            if new_system_prompt:
                self._apply_prompt()
        except Exception as e:
            self._save_feedback.failure("⚠ Partial")
            self.status_lbl.setText(f"Settings saved; live backend apply failed: {e}")
            # UI-TRUST-01B: the backend swap may have half-completed -- refresh
            # so the operator sees the NEW state's truth, never old green text.
            self.backend_connection_changed.emit()
            return

        # UI-TRUST-01B: live backend/model state changed successfully.
        self.backend_connection_changed.emit()
        self._save_feedback.success("✓ Saved")
        self.status_lbl.setText("Settings saved.")
