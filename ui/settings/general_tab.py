from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QCheckBox, QComboBox, QLineEdit

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from core import persistence

from ._widgets import _sec, _lbl, _te, _le, _btn, _spin, _scroll_wrap


# ── Tab: General ───────────────────────────────────────────────────────────────

class GeneralTab(QWidget):
    CLOUD_BACKENDS = {"openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen"}

    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._build()

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
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["llamacpp", "lmstudio", "ollama", "vllm", "openrouter", "deepseek", "groq", "openai", "anthropic", "gemini", "kimi", "qwen", "custom", "omniroute"])
        self.backend_combo.setCurrentText(config.LLM_BACKEND)
        self.backend_combo.setFixedHeight(36)
        self.backend_combo.setStyleSheet(f"QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}QComboBox::drop-down{{border:none;width:20px;}}")
        self.backend_combo.currentTextChanged.connect(self._on_backend_changed)
        be_col.addWidget(self.backend_combo)
        url_col = QVBoxLayout()
        url_col.addWidget(_lbl("Server URL", self.c))
        self.url = _le(config.LLM_BACKEND_URL, self.c)
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
        cm_col.addWidget(self.custom_model)
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
        self.cloud_model = QComboBox()
        self.cloud_model.setEditable(True)
        self.cloud_model.setFixedHeight(36)
        self.cloud_model.setStyleSheet(f"QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}QComboBox::drop-down{{border:none;width:20px;}}")
        model_row.addWidget(self.cloud_model, 1)
        refresh_models_btn = _btn("⟳", self.c)
        refresh_models_btn.setFixedWidth(36)
        refresh_models_btn.setToolTip("Fetch available models from this backend using the API key above")
        refresh_models_btn.clicked.connect(self._refresh_models)
        model_row.addWidget(refresh_models_btn)
        model_col.addLayout(model_row)
        cloud_layout.addLayout(key_col, 2)
        cloud_layout.addLayout(model_col, 2)
        layout.addWidget(self.cloud_widget)
        self._refresh_cloud_row(config.LLM_BACKEND)  # set initial state
        self.url.setReadOnly(config.LLM_BACKEND not in ("custom", "omniroute"))

        layout.addWidget(_sec("CONTEXT WINDOW", self.c))
        layout.addWidget(_lbl(
            "Max Context Tokens, Memory Inject Limit, and Tool Result Max Chars "
            "are saved per-backend — switching backends above recalls that "
            "backend's own values.", self.c
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

        # ── Dreaming ──
        layout.addWidget(_sec("DREAMING", self.c))
        layout.addWidget(_lbl(
            "Idle-sweep background summarization — writes brief session recaps "
            "into memory after a chat sits idle. Was previously only "
            "configurable by editing config.py directly.", self.c
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
        apply_btn = _btn("Apply Change", self.c)
        apply_btn.clicked.connect(self._apply_prompt)
        save_btn = _btn("Save All Settings", self.c, accent=True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(apply_btn)
        btn_row.addStretch()
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)
        layout.addStretch()
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    def _on_dream_toggled(self, checked: bool):
        self.dream_idle_spin.setEnabled(checked)

    def _refresh_cloud_row(self, backend: str):
        is_cloud = backend in self.CLOUD_BACKENDS
        self.cloud_widget.setVisible(is_cloud)
        self.url.setEnabled(not is_cloud)  # URL field irrelevant for cloud
        if is_cloud:
            key_attr = f"{backend.upper()}_API_KEY"
            model_attr = f"{backend.upper()}_DEFAULT_MODEL"
            self.cloud_key.setText(getattr(config, key_attr, ""))
            self.cloud_model.setCurrentText(getattr(config, model_attr, ""))
    def _apply_prompt(self):
        p = self.prompt.toPlainText().strip()
        if not p:
            return
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

    _BACKEND_URLS = {
        "llamacpp":  "http://localhost:8080/v1",
        "lmstudio":  "http://localhost:1234/v1",
        "ollama":    "http://localhost:11434/v1",
        "vllm":      "http://localhost:8000/v1",
        "custom":    "",
        "omniroute": "http://localhost:20128/v1",
    }

    def _on_backend_changed(self, name: str):
        self._refresh_cloud_row(name)
        self.url.setText(self._BACKEND_URLS.get(name, ""))
        is_freeform = name in ("custom", "omniroute")
        self.url.setReadOnly(not is_freeform)
        self.url.setPlaceholderText("Enter your OpenAI-compatible endpoint URL" if is_freeform else "")
        self.custom_model_widget.setVisible(is_freeform)
        if name == "omniroute":
            self.custom_model.setText(config.OMNIROUTE_DEFAULT_MODEL)
            self.custom_api_key.setText(config.OMNIROUTE_API_KEY)
            self.custom_model.setPlaceholderText("e.g. kr/glm-5, if/kimi-k2-thinking")
        elif name == "custom":
            self.custom_model.setText(getattr(config, "CUSTOM_DEFAULT_MODEL", ""))
            self.custom_api_key.setText(getattr(config, "CUSTOM_API_KEY", ""))
            self.custom_model.setPlaceholderText("e.g. mistral-7b-instruct")
        self._refresh_context_row(name)

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

    def _refresh_models(self):
        """Probe the currently-selected cloud backend for its available
        models using whatever API key is typed right now — not necessarily
        saved yet. Backends read their key from the config module at
        construction time (see loader.py), so this temporarily sets
        config.<BACKEND>_API_KEY for the probe call only and restores the
        previous value afterward. Clicking Refresh must have no persistent
        effect until Save is actually pressed — same principle the rest of
        this tab already follows for cloud credentials."""
        backend_name = self.backend_combo.currentText()
        if backend_name not in self.CLOUD_BACKENDS:
            return
        key_attr = f"{backend_name.upper()}_API_KEY"
        prev_key = getattr(config, key_attr, "")
        setattr(config, key_attr, self.cloud_key.text().strip())
        try:
            from core.backends.loader import get_llm_backend
            probe = get_llm_backend(name=backend_name)
            models = probe.list_models()
        except Exception:
            models = []
        finally:
            setattr(config, key_attr, prev_key)

        current = self.cloud_model.currentText()
        self.cloud_model.clear()
        if models:
            self.cloud_model.addItems(models)
        if current:
            self.cloud_model.setCurrentText(current)

    def _save(self):
        from core.backends.loader import get_llm_backend
        new_system_prompt = self.prompt.toPlainText().strip()
        if new_system_prompt:
            config.SYSTEM_PROMPT = new_system_prompt
        config.MAX_CONTEXT_TOKENS = self.ctx_spin.value()
        config.MEMORY_INJECT_LIMIT = self.mem_spin.value()
        config.TOOL_RESULT_MAX_CHARS = self.result_spin.value()
        config.MAX_TOOL_ITERATIONS = self.iter_spin.value()
        config.RESPONSE_RESERVE_TOKENS = self.resp_spin.value()
        config.DREAM_SWEEP_ENABLED = self.dream_enabled_cb.isChecked()
        config.DREAM_IDLE_MINUTES = self.dream_idle_spin.value()
        config.SHOW_THINK_BLOCKS = self.show_think_cb.isChecked()
        config.LLM_BACKEND = self.backend_combo.currentText()
        config.LLM_BACKEND_URL = self.url.text().strip()
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

        # Context/memory settings — max_tool_iterations and
        # response_reserve_tokens are global; max_context_tokens,
        # memory_inject_limit, and tool_result_max_chars are saved per-backend
        # so switching backends recalls each one's own values instead of
        # clobbering the other.
        prefs["max_tool_iterations"] = config.MAX_TOOL_ITERATIONS
        prefs["response_reserve_tokens"] = config.RESPONSE_RESERVE_TOKENS
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

        # S41 fix: cloud API key/model used to only live in the running
        # config module (setattr above) and revert to "" on restart — same
        # bug class the context settings had. Only write an entry when this
        # is actually a cloud backend; local/custom backends don't have one.
        # FE-09: the key itself now goes to secrets.py, not prefs.json —
        # prefs.json gets dragged into Project uploads and the public repo,
        # secrets.py never does. Only default_model stays in prefs.
        if config.LLM_BACKEND in self.CLOUD_BACKENDS:
            from core import secrets as _secrets
            _secrets.set_secret(f"{config.LLM_BACKEND}_api_key", self.cloud_key.text().strip())
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
            prefs["custom_default_model"] = config.CUSTOM_DEFAULT_MODEL
            self.agent.llm._model = config.CUSTOM_DEFAULT_MODEL
        elif config.LLM_BACKEND == "omniroute":
            config.OMNIROUTE_DEFAULT_MODEL = self.custom_model.text().strip()
            config.OMNIROUTE_API_KEY = self.custom_api_key.text().strip()
            from core import secrets as _secrets
            _secrets.set_secret("omniroute_api_key", config.OMNIROUTE_API_KEY)
            prefs["omniroute_default_model"] = config.OMNIROUTE_DEFAULT_MODEL
            self.agent.llm._model = config.OMNIROUTE_DEFAULT_MODEL

        save_prefs(prefs)

        self.agent.llm = get_llm_backend()
        self.agent.llm.base_url = config.LLM_BACKEND_URL

        # Apply context/memory settings to the live agent immediately — no
        # restart needed, matching the backend-swap precedent above.
        # ContextManager captured these at construction time (max_tokens,
        # reserve); update the instance directly, not just the config module,
        # or a hot-reload gap opens up (see F-09: from-import snapshotting).
        self.agent.ctx.max_tokens = config.MAX_CONTEXT_TOKENS
        self.agent.ctx.reserve = config.RESPONSE_RESERVE_TOKENS
        if new_system_prompt:
            self._apply_prompt()
