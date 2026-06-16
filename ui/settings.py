from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QPainterPath
"""
Lumina Settings Panel — tabbed interface.
Tabs: General | Profile | Memory | Knowledge | Tools | TTS | About
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QFrame, QSpinBox, QCheckBox, QScrollArea,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QSizePolicy, QFileDialog, QMessageBox,
    QComboBox, QSlider, QLineEdit, QInputDialog, QDoubleSpinBox
)
from PySide6.QtCore import Qt, Signal, QThread

import os, sys, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from core import persistence


# ── Style helpers ──────────────────────────────────────────────────────────────

def _sec(text: str, c: dict) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{c['accent']};font-size:10px;font-weight:bold;letter-spacing:2px;padding:10px 0 4px 0;background:transparent;")
    return lbl

def _lbl(text: str, c: dict) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{c['text_muted']};font-size:12px;background:transparent;")
    return lbl

def _te(default: str, c: dict, single: bool = False, height: int = None) -> QTextEdit:
    te = QTextEdit()
    te.setPlainText(default)
    if single:
        te.setFixedHeight(36)
        te.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    elif height:
        te.setFixedHeight(height)
    te.setStyleSheet(f"""
        QTextEdit{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:5px 10px;font-size:12px;}}
        QTextEdit:focus{{border:1px solid {c['border_accent']};}}
    """)
    return te

def _le(default: str, c: dict) -> QLineEdit:
    le = QLineEdit(default)
    le.setFixedHeight(36)
    le.setStyleSheet(f"""
        QLineEdit{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:5px 10px;font-size:12px;}}
        QLineEdit:focus{{border:1px solid {c['border_accent']};}}
    """)
    return le

def _btn(text: str, c: dict, accent: bool = False, danger: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    if accent:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['accent']};color:{c['bg_deep']};border:none;
            border-radius:7px;padding:8px 18px;font-size:12px;font-weight:bold;}}
            QPushButton:hover{{background:#33ecff;}}
        """)
    elif danger:
        btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{c['danger']};border:1px solid {c['danger']}44;
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{background:{c['danger']}22;border-color:{c['danger']};}}
        """)
    else:
        btn.setStyleSheet(f"""
            QPushButton{{background:{c['bg_card']};color:{c['text_primary']};border:1px solid {c['border']};
            border-radius:7px;padding:6px 14px;font-size:12px;}}
            QPushButton:hover{{border-color:{c['accent_dim']};color:{c['accent']};}}
        """)
    return btn

def _spin(val: int, lo: int, hi: int, step: int, c: dict) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(val)
    s.setFixedHeight(36)
    s.setStyleSheet(f"""
        QSpinBox{{background:{c['bg_input']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:7px;padding:4px 8px;font-size:12px;}}
    """)
    return s

def _table(cols: list, c: dict) -> QTableWidget:
    t = QTableWidget()
    t.setColumnCount(len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setAlternatingRowColors(False)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(True)
    t.setStyleSheet(f"""
        QTableWidget{{background:{c['bg_card']};color:{c['text_primary']};
        border:1px solid {c['border']};border-radius:8px;gridline-color:{c['border']};font-size:12px;}}
        QTableWidget::item{{padding:6px 10px;border:none;}}
        QTableWidget::item:selected{{background:{c['accent_glow']};color:{c['accent']};}}
        QHeaderView::section{{background:{c['bg_panel']};color:{c['text_muted']};
        border:none;border-bottom:1px solid {c['border']};padding:6px 10px;font-size:11px;font-weight:bold;}}
    """)
    return t

def make_round_pixmap(path: str, size: int) -> QPixmap:
    src = QPixmap(path).scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, src)
    p.end()
    return out

def _scroll_wrap(widget: QWidget, c: dict) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setStyleSheet(f"QScrollArea{{background:{c['bg_deep']};border:none;}}")
    sa.setWidget(widget)
    return sa


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _db():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Tab: General ───────────────────────────────────────────────────────────────

class GeneralTab(QWidget):
    CLOUD_BACKENDS = {"openrouter", "deepseek", "groq", "openai"}

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
        self.backend_combo.addItems(["llamacpp", "lmstudio", "ollama", "vllm", "openrouter", "deepseek", "groq", "openai", "custom"])
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

        # ── Custom model row (hidden unless custom backend selected) ──
        self.custom_model_widget = QWidget()
        cm_layout = QHBoxLayout(self.custom_model_widget)
        cm_layout.setContentsMargins(0, 4, 0, 0)
        cm_layout.setSpacing(12)
        cm_col = QVBoxLayout()
        cm_col.addWidget(_lbl("Model Name", self.c))
        self.custom_model = _le(getattr(config, "CUSTOM_DEFAULT_MODEL", ""), self.c)
        self.custom_model.setPlaceholderText("e.g. mistral-7b-instruct")
        cm_col.addWidget(self.custom_model)
        cm_layout.addLayout(cm_col, 2)
        cm_key_col = QVBoxLayout()
        cm_key_col.addWidget(_lbl("API Key (optional)", self.c))
        self.custom_api_key = _le(getattr(config, "CUSTOM_API_KEY", ""), self.c)
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
        self.cloud_model = _le("", self.c)
        model_col.addWidget(self.cloud_model)
        cloud_layout.addLayout(key_col, 2)
        cloud_layout.addLayout(model_col, 2)
        layout.addWidget(self.cloud_widget)
        self._refresh_cloud_row(config.LLM_BACKEND)  # set initial state
        self.url.setReadOnly(config.LLM_BACKEND != "custom")

        layout.addWidget(_sec("CONTEXT WINDOW", self.c))
        row = QHBoxLayout()
        row.setSpacing(16)
        left = QVBoxLayout()
        left.addWidget(_lbl("Max Context Tokens", self.c))
        self.ctx_spin = _spin(config.MAX_CONTEXT_TOKENS, 1024, 32768, 1024, self.c)
        left.addWidget(self.ctx_spin)
        right = QVBoxLayout()
        right.addWidget(_lbl("Max Tool Iterations", self.c))
        self.iter_spin = _spin(config.MAX_TOOL_ITERATIONS, 1, 20, 1, self.c)
        right.addWidget(self.iter_spin)
        right2 = QVBoxLayout()
        right2.addWidget(_lbl("Response Tokens", self.c))
        self.resp_spin = _spin(config.RESPONSE_RESERVE_TOKENS, 256, 4096, 256, self.c)
        right2.addWidget(self.resp_spin)
        row.addLayout(left)
        row.addLayout(right)
        row.addLayout(right2)
        layout.addLayout(row)
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

    def _refresh_cloud_row(self, backend: str):
        is_cloud = backend in self.CLOUD_BACKENDS
        self.cloud_widget.setVisible(is_cloud)
        self.url.setEnabled(not is_cloud)  # URL field irrelevant for cloud
        if is_cloud:
            key_attr = f"{backend.upper()}_API_KEY"
            model_attr = f"{backend.upper()}_DEFAULT_MODEL"
            self.cloud_key.setText(getattr(config, key_attr, ""))
            self.cloud_model.setText(getattr(config, model_attr, ""))
    def _apply_prompt(self):
        p = self.prompt.toPlainText().strip()
        if p:
            self.agent.ctx.update_system_prompt(p)
            
    _BACKEND_URLS = {
        "llamacpp": "http://localhost:8080/v1",
        "lmstudio": "http://localhost:1234/v1",
        "ollama":   "http://localhost:11434/v1",
        "vllm":     "http://localhost:8000/v1",
        "custom":   "",
    }

    def _on_backend_changed(self, name: str):
        self._refresh_cloud_row(name)
        self.url.setText(self._BACKEND_URLS.get(name, ""))
        is_custom = name == "custom"
        self.url.setReadOnly(not is_custom)
        self.url.setPlaceholderText("Enter your OpenAI-compatible endpoint URL" if is_custom else "")
        self.custom_model_widget.setVisible(is_custom)

    def _save(self):
        from core.backends.loader import get_llm_backend
        config.MAX_CONTEXT_TOKENS = self.ctx_spin.value()
        config.MAX_TOOL_ITERATIONS = self.iter_spin.value()
        config.RESPONSE_RESERVE_TOKENS = self.resp_spin.value()
        config.LLM_BACKEND = self.backend_combo.currentText()
        config.LLM_BACKEND_URL = self.url.text().strip()
        config.LM_STUDIO_BASE_URL = config.LLM_BACKEND_URL
        
        # Cloud credentials
        if config.LLM_BACKEND in self.CLOUD_BACKENDS:
            key_attr = f"{config.LLM_BACKEND.upper()}_API_KEY"
            model_attr = f"{config.LLM_BACKEND.upper()}_DEFAULT_MODEL"
            setattr(config, key_attr, self.cloud_key.text().strip())
            setattr(config, model_attr, self.cloud_model.text().strip())


        from core.persistence import load as load_prefs, save as save_prefs
        prefs = load_prefs()
        prefs["llm_backend"] = config.LLM_BACKEND
        prefs["llm_backend_url"] = config.LLM_BACKEND_URL
        
        if config.LLM_BACKEND == "custom":
            config.CUSTOM_DEFAULT_MODEL = self.custom_model.text().strip()
            config.CUSTOM_API_KEY = self.custom_api_key.text().strip()
            prefs["custom_default_model"] = config.CUSTOM_DEFAULT_MODEL
            prefs["custom_api_key"] = config.CUSTOM_API_KEY
            self.agent.llm._model = config.CUSTOM_DEFAULT_MODEL

        save_prefs(prefs)

        self.agent.llm = get_llm_backend()
        self.agent.llm.base_url = config.LLM_BACKEND_URL


# ── Tab: Profile ───────────────────────────────────────────────────────────────

# ── Tab: User Profile ──────────────────────────────────────────────────────────

class UserProfileTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._prefs = persistence.load()
        self._build()

    def _build(self):
        outer = QWidget()
        outer.setStyleSheet(f"background:{self.c['bg_deep']};")
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(8)

        # ── Your Identity ──
        layout.addWidget(_sec("YOUR IDENTITY", self.c))
        layout.addWidget(_lbl("Your Name", self.c))
        self.user_name = _le(config.USER_NAME, self.c)
        layout.addWidget(self.user_name)

        # ── Bio ──
        layout.addWidget(_sec(f"MY HUMAN ({config.USER_NAME}'s profile)", self.c))
        layout.addWidget(_lbl("Tell Lumina about yourself — this injects into every session.", self.c))
        self.human_bio = _te(
            self._prefs.get("human_bio", f"Name: {config.USER_NAME}\n"),
            self.c, height=180
        )
        layout.addWidget(self.human_bio)
        self.human_bio.textChanged.connect(self._autosave_bio)

        # ── Your Avatar ──
        layout.addWidget(_sec("YOUR AVATAR", self.c))
        av_row = QHBoxLayout()
        av_row.setSpacing(24)

        self.usr_av = self._av_btn("user_avatar_path", config.USER_NAME)
        av_row.addWidget(self.usr_av, alignment=Qt.AlignLeft)

        av_col = QVBoxLayout()
        usr_pick = _btn("Browse...", self.c)
        usr_pick.clicked.connect(
            lambda: self._pick_av("user_avatar_path", self.usr_av, config.USER_NAME)
        )
        av_col.addWidget(usr_pick)
        av_col.addStretch()
        av_row.addLayout(av_col)
        av_row.addStretch()
        layout.addLayout(av_row)

        save_btn = _btn("Save Profile", self.c, accent=True)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)
        layout.addStretch()

        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(_scroll_wrap(outer, self.c))

    def _av_btn(self, key: str, name: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(100, 100)
        path = self._prefs.get(key)
        if path and os.path.exists(path):
            pix = make_round_pixmap(path, 100)
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};
                border-radius:50px;}}
                QPushButton:hover{{border-color:{self.c['accent']};}}
            """)
        else:
            btn.setText(name[0].upper())
            btn.setStyleSheet(f"""
                QPushButton{{background:{self.c['accent_glow']};border:1px solid {self.c['border_accent']};
                border-radius:50px;color:{self.c['accent']};font-size:28px;font-weight:bold;}}
                QPushButton:hover{{background:{self.c['bg_card']};}}
            """)
        return btn

    def _pick_av(self, key: str, btn: QPushButton, name: str):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Select {name} Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self._prefs[key] = path
            persistence.save(self._prefs)
            pix = make_round_pixmap(path, 100)
            btn.setIcon(QIcon(pix))
            btn.setIconSize(pix.size())
            btn.setText("")
            btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};
                border-radius:50px;}}
                QPushButton:hover{{border-color:{self.c['accent']};}}
            """)

    def _save(self):
        config.USER_NAME = self.user_name.text().strip() or config.USER_NAME
        self._prefs["human_bio"] = self.human_bio.toPlainText().strip()
        persistence.save(self._prefs)
        bio = self._prefs["human_bio"]
        if bio:
            import re
            current = self.agent.ctx.system_prompt
            cleaned = re.sub(r"\n\n## About[^\n]*\n.*", "", current, flags=re.DOTALL)
            self.agent.ctx.update_system_prompt(
                cleaned + f"\n\n## About {config.USER_NAME}\n{bio}"
            )

    def _autosave_bio(self):
        self._prefs["human_bio"] = self.human_bio.toPlainText().strip()
        persistence.save(self._prefs)

# ── Tab: Memory ────────────────────────────────────────────────────────────────

class MemoryTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._build()
        self._load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,16,20,16)
        layout.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        top.addWidget(_lbl("Stored Memories", self.c))
        top.addStretch()

        self.filter_le = QLineEdit()
        self.filter_le.setPlaceholderText("Filter by label or keyword...")
        self.filter_le.setFixedHeight(32)
        self.filter_le.setFixedWidth(220)
        self.filter_le.setStyleSheet(f"QLineEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:6px;padding:4px 10px;font-size:12px;}}QLineEdit:focus{{border:1px solid {self.c['border_accent']};}}")
        self.filter_le.textChanged.connect(self._filter)
        top.addWidget(self.filter_le)

        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # Table
        self.table = _table(["ID", "Label", "Content", "Created"], self.c)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 400)
        self.table.setColumnWidth(3, 120)
        layout.addWidget(self.table, 1)

        # Add memory row
        add_frame = QFrame()
        add_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(12,10,12,10)
        add_layout.setSpacing(6)
        add_layout.addWidget(_lbl("Add Memory", self.c))

        inp_row = QHBoxLayout()
        self.new_label = _le("general", self.c)
        self.new_label.setPlaceholderText("label")
        self.new_label.setFixedWidth(120)
        self.new_content = _le("", self.c)
        self.new_content.setPlaceholderText("Memory content (max 512 chars)...")
        add_mem_btn = _btn("Save", self.c, accent=True)
        add_mem_btn.setFixedWidth(70)
        add_mem_btn.clicked.connect(self._add_memory)
        inp_row.addWidget(self.new_label)
        inp_row.addWidget(self.new_content, 1)
        inp_row.addWidget(add_mem_btn)
        add_layout.addLayout(inp_row)
        layout.addWidget(add_frame)

        # Bottom actions
        bot = QHBoxLayout()
        del_btn = _btn("Delete Selected", self.c, danger=True)
        del_btn.clicked.connect(self._delete_selected)
        paste_btn = _btn("Paste from Sapphire...", self.c)
        paste_btn.setToolTip("Paste a block of memories exported from Sapphire")
        paste_btn.clicked.connect(self._paste_import)
        bot.addWidget(del_btn)
        bot.addWidget(paste_btn)
        bot.addStretch()
        layout.addLayout(bot)

    def _load(self):
        conn = _db()
        rows = conn.execute("SELECT id, label, content, created_at FROM memories ORDER BY created_at DESC").fetchall()
        conn.close()
        self._all_rows = [dict(r) for r in rows]
        self._render(self._all_rows)

    def _render(self, rows: list):
        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(r["label"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["content"][:120]))
            self.table.setItem(i, 3, QTableWidgetItem(r["created_at"][:16]))

    def _filter(self, text: str):
        if not text:
            self._render(self._all_rows)
            return
        t = text.lower()
        filtered = [r for r in self._all_rows if t in r["label"].lower() or t in r["content"].lower()]
        self._render(filtered)

    def _add_memory(self):
        content = self.new_content.text().strip()
        label = self.new_label.text().strip() or "general"
        if not content:
            return
        from tools.memory import save_memory
        save_memory(content, label)
        self.new_content.clear()
        self._load()

    def _delete_selected(self):
        rows = self.table.selectedItems()
        if not rows:
            return
        ids = list({self.table.item(r.row(), 0).text() for r in rows})
        reply = QMessageBox.question(self, "Delete", f"Delete {len(ids)} memory entries?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            conn = _db()
            for mid in ids:
                conn.execute("DELETE FROM memories WHERE id=?", (int(mid),))
            conn.commit()
            conn.close()
            self._load()

    def _paste_import(self):
        """Import memories pasted as plain text — one per line or JSON array."""
        text, ok = QInputDialog.getMultiLineText(
            self, "Import Memories",
            "Paste memories (one per line, or JSON array of {content, label} objects):"
        )
        if not ok or not text.strip():
            return
        from tools.memory import save_memory
        count = 0
        # Try JSON first
        try:
            items = json.loads(text.strip())
            for item in items:
                if isinstance(item, dict):
                    save_memory(item.get("content",""), item.get("label","imported"))
                    count += 1
                elif isinstance(item, str):
                    save_memory(item, "imported")
                    count += 1
        except Exception:
            # Plain text — one memory per line
            for line in text.strip().splitlines():
                line = line.strip()
                if line:
                    save_memory(line, "imported")
                    count += 1
        self._load()
        QMessageBox.information(self, "Import Complete", f"Imported {count} memories.")


# ── Tab: Knowledge ─────────────────────────────────────────────────────────────

class KnowledgeTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._build()
        self._load()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20,16,20,16)
        layout.setSpacing(10)

        # Top bar
        top = QHBoxLayout()
        top.addWidget(_lbl("Knowledge Base", self.c))
        top.addStretch()
        self.cat_filter = QLineEdit()
        self.cat_filter.setPlaceholderText("Filter category...")
        self.cat_filter.setFixedHeight(32)
        self.cat_filter.setFixedWidth(180)
        self.cat_filter.setStyleSheet(f"QLineEdit{{background:{self.c['bg_input']};color:{self.c['text_primary']};border:1px solid {self.c['border']};border-radius:6px;padding:4px 10px;font-size:12px;}}QLineEdit:focus{{border:1px solid {self.c['border_accent']};}}")
        self.cat_filter.textChanged.connect(self._filter)
        top.addWidget(self.cat_filter)
        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(32)
        refresh_btn.clicked.connect(self._load)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # Table
        self.table = _table(["ID", "Category", "Title", "Content", "Updated"], self.c)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 320)
        layout.addWidget(self.table, 1)

        # Add entry
        add_frame = QFrame()
        add_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(12,10,12,10)
        add_layout.setSpacing(6)
        add_layout.addWidget(_lbl("Add Knowledge Entry", self.c))

        meta_row = QHBoxLayout()
        self.new_cat = _le("notes", self.c)
        self.new_cat.setPlaceholderText("category")
        self.new_cat.setFixedWidth(120)
        self.new_title = _le("", self.c)
        self.new_title.setPlaceholderText("title (optional)")
        self.new_title.setFixedWidth(160)
        meta_row.addWidget(self.new_cat)
        meta_row.addWidget(self.new_title)
        meta_row.addStretch()
        add_layout.addLayout(meta_row)

        self.new_content = _te("", self.c, height=80)
        self.new_content.setPlaceholderText("Paste content here — large blocks will be chunked automatically...")
        add_layout.addWidget(self.new_content)

        btn_row = QHBoxLayout()
        save_btn = _btn("Save Entry", self.c, accent=True)
        save_btn.clicked.connect(self._add_entry)
        file_btn = _btn("📄 Upload File", self.c)
        file_btn.clicked.connect(self._upload_file)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(file_btn)
        btn_row.addStretch()
        add_layout.addLayout(btn_row)
        layout.addWidget(add_frame)

        # Bottom actions
        bot = QHBoxLayout()
        del_btn = _btn("Delete Selected", self.c, danger=True)
        del_btn.clicked.connect(self._delete_selected)
        bot.addWidget(del_btn)
        bot.addStretch()
        layout.addLayout(bot)

    def _load(self):
        conn = _db()
        try:
            rows = conn.execute("SELECT id, category, title, content, updated_at FROM knowledge ORDER BY updated_at DESC").fetchall()
            self._all_rows = [dict(r) for r in rows]
        except Exception:
            self._all_rows = []
        conn.close()
        self._render(self._all_rows)

    def _render(self, rows: list):
        self.table.setRowCount(0)
        for r in rows:
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.table.setItem(i, 1, QTableWidgetItem(r.get("category", "")))
            self.table.setItem(i, 2, QTableWidgetItem(r.get("title") or ""))
            self.table.setItem(i, 3, QTableWidgetItem(r.get("content","")[:100]))
            self.table.setItem(i, 4, QTableWidgetItem(str(r.get("updated_at",""))[:16]))

    def _filter(self, text: str):
        if not text:
            self._render(self._all_rows)
            return
        t = text.lower()
        filtered = [r for r in self._all_rows if t in r.get("category","").lower() or t in r.get("title","").lower()]
        self._render(filtered)

    def _add_entry(self):
        content = self.new_content.toPlainText().strip()
        cat = self.new_cat.text().strip() or "notes"
        title = self.new_title.text().strip() or None
        if not content:
            return
        from tools.knowledge import save_knowledge
        # Chunk large content (>2000 chars)
        if len(content) > 2000:
            chunks = [content[i:i+1800] for i in range(0, len(content), 1800)]
            for idx, chunk in enumerate(chunks):
                t = f"{title} (part {idx+1})" if title else f"chunk {idx+1}"
                save_knowledge(cat, chunk, t)
        else:
            save_knowledge(cat, content, title)
        self.new_content.clear()
        self._load()

    def _upload_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Upload to Knowledge Base", "",
            "Text files (*.txt *.md *.py *.json *.csv);;All files (*)"
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            cat = self.new_cat.text().strip() or "files"
            title = os.path.basename(path)
            from tools.knowledge import save_knowledge
            if len(content) > 2000:
                chunks = [content[i:i+1800] for i in range(0, len(content), 1800)]
                for idx, chunk in enumerate(chunks):
                    save_knowledge(cat, chunk, f"{title} (part {idx+1})")
            else:
                save_knowledge(cat, content, title)
            self._load()
            QMessageBox.information(self, "Uploaded", f"'{title}' added to knowledge base.")
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not read file: {e}")

    def _delete_selected(self):
        rows = self.table.selectedItems()
        if not rows:
            return
        ids = list({self.table.item(r.row(), 0).text() for r in rows})
        reply = QMessageBox.question(self, "Delete", f"Delete {len(ids)} entries?",
                                     QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            conn = _db()
            for eid in ids:
                conn.execute("DELETE FROM knowledge WHERE id=?", (int(eid),))
            conn.commit()
            conn.close()
            self._load()


# ── Tab: Tools ─────────────────────────────────────────────────────────────────

class ToolsTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._prefs = persistence.load()
        self._current_profile_path = None
        self._build()
        self._load_profiles()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        # ── Profile bar ──
        profile_frame = QFrame()
        profile_frame.setStyleSheet(f"QFrame{{background:{self.c['bg_card']};border:1px solid {self.c['border']};border-radius:8px;}}")
        profile_layout = QHBoxLayout(profile_frame)
        profile_layout.setContentsMargins(12, 8, 12, 8)
        profile_layout.setSpacing(8)

        profile_layout.addWidget(_lbl("Profile:", self.c))

        self.profile_combo = QComboBox()
        self.profile_combo.setFixedHeight(32)
        self.profile_combo.setMinimumWidth(180)
        self.profile_combo.setStyleSheet(f"""
            QComboBox{{background:{self.c['bg_input']};color:{self.c['text_primary']};
            border:1px solid {self.c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        profile_layout.addWidget(self.profile_combo)

        self.count_lbl = _lbl("", self.c)
        profile_layout.addWidget(self.count_lbl)
        profile_layout.addStretch()

        new_btn = _btn("＋ New", self.c)
        new_btn.setFixedHeight(30)
        new_btn.clicked.connect(self._new_profile)
        profile_layout.addWidget(new_btn)

        save_profile_btn = _btn("💾 Save", self.c, accent=True)
        save_profile_btn.setFixedHeight(30)
        save_profile_btn.clicked.connect(self._save_profile)
        profile_layout.addWidget(save_profile_btn)

        self.del_profile_btn = _btn("✕ Delete", self.c, danger=True)
        self.del_profile_btn.setFixedHeight(30)
        self.del_profile_btn.clicked.connect(self._delete_profile)
        profile_layout.addWidget(self.del_profile_btn)

        layout.addWidget(profile_frame)

        # ── Tool table header ──
        top = QHBoxLayout()
        top.addStretch()

        refresh_btn = _btn("⟳ Refresh", self.c)
        refresh_btn.setFixedHeight(30)
        refresh_btn.clicked.connect(self._load_tools)
        top.addWidget(refresh_btn)

        disable_all_btn = _btn("Disable All", self.c, danger=True)
        disable_all_btn.setFixedHeight(30)
        disable_all_btn.clicked.connect(self._disable_all)
        top.addWidget(disable_all_btn)

        enable_all_btn = _btn("Enable All", self.c, accent=True)
        enable_all_btn.setFixedHeight(30)
        enable_all_btn.clicked.connect(self._enable_all)
        top.addWidget(enable_all_btn)
        layout.addLayout(top)

        # ── Tool table ──
        self.table = _table(["Tool", "Description", "Enabled"], self.c)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 460)
        self.table.setColumnWidth(2, 70)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        note = QLabel("Select a profile to load its tool set. Save to write changes back to the profile file.")
        note.setStyleSheet(f"color:{self.c['text_dim']};font-size:11px;font-style:italic;background:transparent;")
        layout.addWidget(note)

    def _load_profiles(self):
        """Populate the profile dropdown from tool_profiles/ directory."""
        from core.tool_profiles import list_profiles, profile_display_name
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self._profiles = list_profiles()
        for p in self._profiles:
            self.profile_combo.addItem(profile_display_name(p), p["_file"])
        self.profile_combo.blockSignals(False)
        # Load current live tool state into table without selecting a profile
        self._load_tools()

    def _on_profile_selected(self, idx: int):
        if idx < 0 or idx >= len(self._profiles):
            return
        profile = self._profiles[idx]
        self._current_profile_path = profile["_file"]
        enabled_set = set(profile.get("enabled", []))
        # Apply to agent registry immediately
        all_tools = list(self.agent.registry._tools.keys())
        disabled = [t for t in all_tools if t not in enabled_set]
        self.agent.registry.set_disabled(disabled)
        self._save_state()
        self._load_tools()

    def _load_tools(self):
        """Render tool table reflecting current agent registry state."""
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        tools = self.agent.registry._tools
        disabled = set(self.agent.registry.get_disabled())
        enabled_count = 0

        for name, data in tools.items():
            i = self.table.rowCount()
            self.table.insertRow(i)
            self.table.setItem(i, 0, QTableWidgetItem(name))
            desc = data["schema"]["function"].get("description", "")
            self.table.setItem(i, 1, QTableWidgetItem(desc))
            cb = QCheckBox()
            cb.setChecked(name not in disabled)
            cb.setStyleSheet("QCheckBox { margin-left: 22px; }")
            cb.stateChanged.connect(lambda state, n=name: self._toggle(n, state))
            self.table.setCellWidget(i, 2, cb)
            if name not in disabled:
                enabled_count += 1

        total = len(tools)
        self.count_lbl.setText(f"({enabled_count}/{total})")
        self.table.blockSignals(False)

    def _toggle(self, name: str, state: int):
        if state == Qt.Checked:
            self.agent.registry.enable(name)
        else:
            self.agent.registry.disable(name)
        self._save_state()
        self._update_count()

    def _disable_all(self):
        for name in self.agent.registry.list_tools():
            self.agent.registry.disable(name)
        self._save_state()
        self._load_tools()

    def _enable_all(self):
        for name in self.agent.registry.list_tools():
            self.agent.registry.enable(name)
        self._save_state()
        self._load_tools()

    def _update_count(self):
        total = len(self.agent.registry.list_tools())
        enabled = len(self.agent.registry.list_enabled())
        self.count_lbl.setText(f"({enabled}/{total})")

    def _save_state(self):
        """Persist disabled tool list to prefs.json."""
        self._prefs = persistence.load()
        self._prefs["disabled_tools"] = self.agent.registry.get_disabled()
        persistence.save(self._prefs)

    def _save_profile(self):
        """Write current tool state back to the selected profile JSON."""
        from core.tool_profiles import save_profile, list_profiles, profile_display_name
        if not self._current_profile_path:
            QMessageBox.warning(self, "No Profile", "Select a profile first.")
            return
        enabled = self.agent.registry.list_enabled()
        # Load existing profile to preserve name/description
        try:
            from core.tool_profiles import load_profile
            data = load_profile(self._current_profile_path)
        except Exception:
            data = {}
        data["enabled"] = enabled
        save_profile(self._current_profile_path, data)
        # Refresh dropdown to update count
        current_path = self._current_profile_path
        self._load_profiles()
        # Reselect the same profile
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == current_path:
                self.profile_combo.blockSignals(True)
                self.profile_combo.setCurrentIndex(i)
                self.profile_combo.blockSignals(False)
                break
        self._current_profile_path = current_path

    def _new_profile(self):
        """Create a new empty tool profile."""
        from core.tool_profiles import save_profile, PROFILES_DIR, fname_from_name, list_profiles, profile_display_name
        name, ok = QInputDialog.getText(self, "New Tool Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        fname = fname_from_name(name)
        path = os.path.join(PROFILES_DIR, fname)
        if os.path.exists(path):
            QMessageBox.warning(self, "Exists", f"A profile named '{name}' already exists.")
            return
        data = {"name": name, "description": "", "enabled": []}
        save_profile(path, data)
        self._load_profiles()
        # Select the new profile
        for i in range(self.profile_combo.count()):
            if self.profile_combo.itemData(i) == path:
                self.profile_combo.setCurrentIndex(i)
                break

    def _delete_profile(self):
        """Delete the currently selected profile file."""
        from core.tool_profiles import delete_profile
        if not self._current_profile_path:
            return
        idx = self.profile_combo.currentIndex()
        if idx < 0 or idx >= len(self._profiles):
            return
        name = self._profiles[idx].get("name", "this profile")
        reply = QMessageBox.question(
            self, "Delete Profile",
            f"Delete '{name}'? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            delete_profile(self._current_profile_path)
            self._current_profile_path = None
            self._load_profiles()

# ── Tab: TTS ───────────────────────────────────────────────────────────────────

class TTSTab(QWidget):
    backend_changed = Signal(str)
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
        self.tts_backend_combo.addItems(["kokoro", "voicebox", "chatterbox", "supertonic", "piper"])
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
        self.url.setText(self._BACKEND_URLS.get(name, ""))
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
        try:
            from tts.loader import get_tts_backend
            import config as _c
            _c.TTS_BACKEND = self.tts_backend_combo.currentText()
            _c.TTS_HOST = self.url.text().strip()
            _c.VOICEBOX_HOST = self.url.text().strip()
            bridge = get_tts_backend(force_reload=True)
            bridge.enabled = True
            if bridge.test():
                bridge.speak("Lumina TTS test successful.", blocking=False)
                self.status_lbl.setText("✓ TTS server reachable — playing test audio.")
            else:
                self.status_lbl.setText(f"✗ TTS server not reachable. Is {_c.TTS_BACKEND} running?")
        except Exception as e:
            self.status_lbl.setText(f"✗ Error: {e}")
    def _save(self):
        config.TTS_ENABLED = self.enabled_cb.isChecked()
        config.TTS_HOST = self.url.text().strip()
        config.TTS_BACKEND = self.tts_backend_combo.currentText()
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
            from tts.loader import get_tts_backend
            self.agent.tts = get_tts_backend(force_reload=True)

        self.status_lbl.setText("Settings saved.")

# ── Tab: Personas ─────────────────────────────────────────────────────────────

class PersonasTab(QWidget):
    def __init__(self, agent, c: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.c = c
        self._current_path = None
        self._current_persona = None
        self._build()
        self._load_personas()

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Left sidebar ──
        sidebar = QFrame()
        sidebar.setFixedWidth(220)
        sidebar.setStyleSheet(f"QFrame{{background:{self.c['bg_sidebar']};border-right:1px solid {self.c['border']};}}")
        sb_layout = QVBoxLayout(sidebar)
        sb_layout.setContentsMargins(0, 0, 0, 0)
        sb_layout.setSpacing(0)

        # Sidebar header
        sb_header = QFrame()
        sb_header.setFixedHeight(48)
        sb_header.setStyleSheet(f"QFrame{{background:{self.c['bg_panel']};border-bottom:1px solid {self.c['border']};}}")
        sb_header_layout = QHBoxLayout(sb_header)
        sb_header_layout.setContentsMargins(12, 0, 8, 0)
        sb_lbl = QLabel("PERSONAS")
        sb_lbl.setStyleSheet(f"color:{self.c['accent']};font-size:10px;font-weight:bold;letter-spacing:2px;background:transparent;")
        sb_header_layout.addWidget(sb_lbl)
        sb_header_layout.addStretch()

        new_btn = QPushButton("＋")
        new_btn.setFixedSize(28, 28)
        new_btn.setCursor(Qt.PointingHandCursor)
        new_btn.setToolTip("New Persona")
        new_btn.setStyleSheet(f"""
            QPushButton{{background:{self.c['accent_glow']};color:{self.c['accent']};
            border:1px solid {self.c['border_accent']};border-radius:6px;font-size:16px;}}
            QPushButton:hover{{background:{self.c['accent']};color:{self.c['bg_deep']};}}
        """)
        new_btn.clicked.connect(self._new_persona)
        sb_header_layout.addWidget(new_btn)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedSize(28, 28)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setToolTip("Refresh personas from disk")
        refresh_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;color:{self.c['text_muted']};
            border:1px solid {self.c['border']};border-radius:6px;font-size:14px;}}
            QPushButton:hover{{color:{self.c['accent']};border-color:{self.c['border_accent']};}}
        """)
        refresh_btn.clicked.connect(self._load_personas)
        sb_header_layout.addWidget(refresh_btn)
        sb_layout.addWidget(sb_header)

        # Persona list scroll area
        self.persona_scroll = QScrollArea()
        self.persona_scroll.setWidgetResizable(True)
        self.persona_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.persona_scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")

        self.persona_list_widget = QWidget()
        self.persona_list_widget.setStyleSheet("background:transparent;")
        self.persona_list_layout = QVBoxLayout(self.persona_list_widget)
        self.persona_list_layout.setContentsMargins(0, 4, 0, 4)
        self.persona_list_layout.setSpacing(0)
        self.persona_list_layout.addStretch()

        self.persona_scroll.setWidget(self.persona_list_widget)
        sb_layout.addWidget(self.persona_scroll, 1)
        root.addWidget(sidebar)

        # ── Right panel ──
        right = QScrollArea()
        right.setWidgetResizable(True)
        right.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        right.setStyleSheet(f"QScrollArea{{background:{self.c['bg_deep']};border:none;}}")

        self.right_widget = QWidget()
        self.right_widget.setStyleSheet(f"background:{self.c['bg_deep']};")
        self.right_layout = QVBoxLayout(self.right_widget)
        self.right_layout.setContentsMargins(28, 24, 28, 24)
        self.right_layout.setSpacing(10)

        # Placeholder
        self.placeholder = QLabel("← Select a persona or create a new one")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color:{self.c['text_dim']};font-size:13px;background:transparent;")
        self.right_layout.addWidget(self.placeholder)
        self.right_layout.addStretch()

        right.setWidget(self.right_widget)
        root.addWidget(right, 1)

        # Store right panel ref for rebuilding
        self._right_scroll = right

    def _load_personas(self):
        """Reload persona list from disk."""
        from core.personas import list_personas
        self._personas = list_personas()

        # Clear list
        while self.persona_list_layout.count() > 1:
            item = self.persona_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for p in self._personas:
            self.persona_list_layout.insertWidget(
                self.persona_list_layout.count() - 1,
                self._make_persona_card(p)
            )

        # Reselect current if still exists
        if self._current_path:
            still_exists = any(p["_file"] == self._current_path for p in self._personas)
            if not still_exists:
                self._current_path = None
                self._current_persona = None
                self._show_placeholder()

    def _make_persona_card(self, persona: dict) -> QFrame:
        path = persona["_file"]
        is_selected = path == self._current_path

        card = QFrame()
        card.setFixedHeight(64)
        card.setCursor(Qt.PointingHandCursor)
        card.setStyleSheet(f"""
            QFrame{{
                background:{'#1a1d28' if is_selected else 'transparent'};
                border:none;
                border-left: 3px solid {'#00e5ff' if is_selected else 'transparent'};
            }}
            QFrame:hover{{background:{self.c['bg_card']};}}
        """)

        layout = QHBoxLayout(card)
        layout.setContentsMargins(8, 8, 6, 8)
        layout.setSpacing(10)

        # Avatar circle
        av_lbl = QLabel()
        av_lbl.setFixedSize(40, 40)
        avatar_path = persona.get("avatar", "")
        if avatar_path:
            if not os.path.isabs(avatar_path):
                avatar_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    avatar_path
                )
        if avatar_path and os.path.exists(avatar_path):
            pix = make_round_pixmap(avatar_path, 40)
            av_lbl.setPixmap(pix)
        else:
            av_lbl.setText(persona.get("name", "?")[0].upper())
            av_lbl.setAlignment(Qt.AlignCenter)
            av_lbl.setStyleSheet(f"""
                background:{self.c['accent_glow']};border:1px solid {self.c['border_accent']};
                border-radius:20px;color:{self.c['accent']};font-size:16px;font-weight:bold;
            """)
        layout.addWidget(av_lbl)

        # Name + tagline
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name_lbl = QLabel(persona.get("name", "unnamed"))
        name_lbl.setStyleSheet(f"color:{'#00e5ff' if is_selected else self.c['text_primary']};font-size:12px;font-weight:bold;background:transparent;")
        tagline = QLabel(persona.get("tagline", ""))
        tagline.setStyleSheet(f"color:{self.c['text_dim']};font-size:10px;background:transparent;")
        tagline.setWordWrap(False)
        text_col.addWidget(name_lbl)
        text_col.addWidget(tagline)
        layout.addLayout(text_col, 1)

        # Click handler
        card.mousePressEvent = lambda e, p=persona: self._select_persona(p)
        return card

    def _show_placeholder(self):
        self._clear_right()
        self.placeholder = QLabel("← Select a persona or create a new one")
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f"color:{self.c['text_dim']};font-size:13px;background:transparent;")
        self.right_layout.addWidget(self.placeholder)
        self.right_layout.addStretch()

    def _clear_right(self):
        while self.right_layout.count():
            item = self.right_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _select_persona(self, persona: dict):
        self._current_path = persona["_file"]
        self._current_persona = persona
        self._load_personas()  # Refresh cards to update selection highlight
        self._build_right_panel(persona)

    def _build_right_panel(self, persona: dict):
        from core.tool_profiles import list_profiles, profile_display_name
        self._clear_right()
        c = self.c
        layout = self.right_layout
        protected = persona.get("protected", False)
        

        # ── Top: avatar + name + tagline + action buttons ──
        top_row = QHBoxLayout()
        top_row.setSpacing(20)

        # Large avatar
        self.rp_av_btn = QPushButton()
        self.rp_av_btn.setFixedSize(120, 120)
        self.rp_av_btn.setCursor(Qt.PointingHandCursor)
        self.rp_av_btn.setToolTip("Click to change avatar")
        avatar_path = persona.get("avatar", "")
        if avatar_path and not os.path.isabs(avatar_path):
            avatar_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                avatar_path
            )
        if avatar_path and os.path.exists(avatar_path):
            pix = make_round_pixmap(avatar_path, 120)
            self.rp_av_btn.setIcon(QIcon(pix))
            self.rp_av_btn.setIconSize(pix.size())
            self.rp_av_btn.setStyleSheet(f"""
                QPushButton{{background:transparent;border:2px solid {c['border_accent']};border-radius:60px;}}
                QPushButton:hover{{border-color:{c['accent']};}}
            """)
        else:
            self.rp_av_btn.setText(persona.get("name", "?")[0].upper())
            self.rp_av_btn.setStyleSheet(f"""
                QPushButton{{background:{c['accent_glow']};border:1px solid {c['border_accent']};
                border-radius:60px;color:{c['accent']};font-size:40px;font-weight:bold;}}
                QPushButton:hover{{background:{c['bg_card']};}}
            """)
        self.rp_av_btn.clicked.connect(self._pick_avatar)
        top_row.addWidget(self.rp_av_btn)

        # Name + tagline + buttons
        meta_col = QVBoxLayout()
        meta_col.setSpacing(6)

        self.rp_name = _le(persona.get("name", ""), c)
        self.rp_name.setStyleSheet(f"""
            QLineEdit{{background:{c['bg_input']};color:{c['accent']};
            border:1px solid {c['border']};border-radius:7px;
            padding:5px 10px;font-size:16px;font-weight:bold;}}
            QLineEdit:focus{{border:1px solid {c['border_accent']};}}
        """)
        meta_col.addWidget(self.rp_name)

        self.rp_tagline = _le(persona.get("tagline", ""), c)
        self.rp_tagline.setPlaceholderText("Short tagline...")
        meta_col.addWidget(self.rp_tagline)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        activate_btn = _btn("⚡ Activate", c, accent=True)
        activate_btn.clicked.connect(self._activate)
        btn_row.addWidget(activate_btn)

        duplicate_btn = _btn("⧉ Duplicate", c)
        duplicate_btn.clicked.connect(self._duplicate)
        btn_row.addWidget(duplicate_btn)

        export_btn = _btn("↑ Export", c)
        export_btn.clicked.connect(self._export)
        btn_row.addWidget(export_btn)

        if not protected:
            delete_btn = _btn("✕ Delete", c, danger=True)
            delete_btn.clicked.connect(self._delete)
            btn_row.addWidget(delete_btn)

        btn_row.addStretch()
        meta_col.addLayout(btn_row)
        meta_col.addStretch()
        top_row.addLayout(meta_col, 1)
        layout.addLayout(top_row)

        # ── System Prompt ──
        layout.addWidget(_sec("SYSTEM PROMPT", c))
        self.rp_prompt = _te(persona.get("system_prompt", ""), c, height=140)
        layout.addWidget(self.rp_prompt)
        
        if protected:
            self.rp_name.setReadOnly(True)
            self.rp_tagline.setReadOnly(True)
            self.rp_prompt.setReadOnly(True)
        if protected:
            locked_style = f"background:{self.c['bg_panel']};color:{self.c['text_dim']};border:1px solid {self.c['border']};border-radius:7px;padding:5px 10px;font-size:12px;"
            self.rp_name.setStyleSheet(locked_style)
            self.rp_tagline.setStyleSheet(locked_style)
            self.rp_prompt.setStyleSheet(locked_style)    

        # ── Tools Profile ──
        layout.addWidget(_sec("TOOLS PROFILE", c))
        tools_row = QHBoxLayout()
        self.rp_tools_combo = QComboBox()
        self.rp_tools_combo.setFixedHeight(36)
        self.rp_tools_combo.setStyleSheet(f"""
            QComboBox{{background:{c['bg_input']};color:{c['text_primary']};
            border:1px solid {c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        profiles = list_profiles()
        self.rp_tools_combo.addItem("— none —", None)
        for p in profiles:
            self.rp_tools_combo.addItem(profile_display_name(p), p["_file"])
        # Select current
        current_tools = (persona.get("tools_profile", "") or "").strip().lower()
        matched = False
        if current_tools:
            for i in range(self.rp_tools_combo.count()):
                item_data = self.rp_tools_combo.itemData(i)
                if item_data and os.path.splitext(os.path.basename(item_data))[0].lower() == current_tools:
                    self.rp_tools_combo.setCurrentIndex(i)
                    matched = True
                    break
            if not matched:
                # fallback: display name case-insensitive startswith
                for i in range(self.rp_tools_combo.count()):
                    if self.rp_tools_combo.itemText(i).lower().startswith(current_tools.split(" (")[0]):
                        self.rp_tools_combo.setCurrentIndex(i)
                        break
        tools_row.addWidget(self.rp_tools_combo, 1)
        layout.addLayout(tools_row)

        # ── TTS ──
        layout.addWidget(_sec("TTS VOICE", c))
        tts_row = QHBoxLayout()
        tts_row.setSpacing(12)

        voice_col = QVBoxLayout()
        voice_col.addWidget(_lbl("Voice", c))
        self.rp_voice = QComboBox()
        self.rp_voice.setFixedHeight(34)
        self.rp_voice.setStyleSheet(f"""
            QComboBox{{background:{c['bg_input']};color:{c['text_primary']};
            border:1px solid {c['border']};border-radius:7px;padding:4px 10px;font-size:12px;}}
            QComboBox::drop-down{{border:none;width:20px;}}
        """)
        voices = self._fetch_voices()
        self.rp_voice.addItems(voices)
        current_voice = persona.get("tts_voice", config.TTS_VOICE)
        if current_voice in voices:
            self.rp_voice.setCurrentText(current_voice)
        voice_col.addWidget(self.rp_voice)
        tts_row.addLayout(voice_col, 2)
        
    

        # Speed
        spd_col = QVBoxLayout()
        self.rp_speed_lbl = QLabel(f"Speed: {persona.get('tts_speed', 1.0):.1f}x")
        self.rp_speed_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_speed = QSlider(Qt.Horizontal)
        self.rp_speed.setRange(50, 200)
        self.rp_speed.setValue(int(persona.get("tts_speed", 1.0) * 100))
        self.rp_speed.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_speed.valueChanged.connect(lambda v: self.rp_speed_lbl.setText(f"Speed: {v/100:.1f}x"))
        spd_col.addWidget(self.rp_speed_lbl)
        spd_col.addWidget(self.rp_speed)
        tts_row.addLayout(spd_col, 2)

        # Pitch
        pch_col = QVBoxLayout()
        self.rp_pitch_lbl = QLabel(f"Pitch: {persona.get('tts_pitch', 1.0):.1f}x")
        self.rp_pitch_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_pitch = QSlider(Qt.Horizontal)
        self.rp_pitch.setRange(50, 200)
        self.rp_pitch.setValue(int(persona.get("tts_pitch", 1.0) * 100))
        self.rp_pitch.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_pitch.valueChanged.connect(lambda v: self.rp_pitch_lbl.setText(f"Pitch: {v/100:.1f}x"))
        pch_col.addWidget(self.rp_pitch_lbl)
        pch_col.addWidget(self.rp_pitch)
        tts_row.addLayout(pch_col, 2)

        # Volume
        vol_col = QVBoxLayout()
        self.rp_vol_lbl = QLabel(f"Volume: {persona.get('tts_volume', 1.0):.1f}x")
        self.rp_vol_lbl.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        self.rp_vol = QSlider(Qt.Horizontal)
        self.rp_vol.setRange(50, 200)
        self.rp_vol.setValue(int(persona.get("tts_volume", 1.0) * 100))
        self.rp_vol.setStyleSheet(f"""
            QSlider::groove:horizontal{{background:{c['border']};height:4px;border-radius:2px;}}
            QSlider::handle:horizontal{{background:{c['accent']};width:14px;height:14px;border-radius:7px;margin:-5px 0;}}
            QSlider::sub-page:horizontal{{background:{c['accent_dim']};height:4px;border-radius:2px;}}
        """)
        self.rp_vol.valueChanged.connect(lambda v: self.rp_vol_lbl.setText(f"Volume: {v/100:.1f}x"))
        vol_col.addWidget(self.rp_vol_lbl)
        vol_col.addWidget(self.rp_vol)
        tts_row.addLayout(vol_col, 2)

        layout.addLayout(tts_row)

        # TTS test button
        tts_btn_row = QHBoxLayout()
        test_tts_btn = _btn("▶ Test Voice", c)
        test_tts_btn.clicked.connect(self._test_tts)
        tts_btn_row.addWidget(test_tts_btn)
        tts_btn_row.addStretch()
        layout.addLayout(tts_btn_row)

        # ── Description / Notes ──
        layout.addWidget(_sec("DESCRIPTION & NOTES", c))
        layout.addWidget(_lbl("Use case, model settings, notes — anything relevant to this persona.", c))
        self.rp_desc = _te(persona.get("description", ""), c, height=100)
        layout.addWidget(self.rp_desc)

        # ── Save button ──
        save_row = QHBoxLayout()
        save_btn = _btn("💾 Save Persona", c, accent=True)
        save_btn.clicked.connect(self._save_persona)
        save_row.addStretch()
        save_row.addWidget(save_btn)
        layout.addLayout(save_row)
        layout.addStretch()

    def refresh_voices(self):
        if not hasattr(self, 'rp_voice'):
            return
        voices = self._fetch_voices()
        current = self.rp_voice.currentText()
        self.rp_voice.clear()
        self.rp_voice.addItems(voices)
        if current in voices:
            self.rp_voice.setCurrentText(current)
    # ── Actions ───────────────────────────────────────────────────────────────

    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Avatar", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if not path or not self._current_persona:
            return
        # Make path relative to lumina root if possible
        lumina_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        try:
            rel = os.path.relpath(path, lumina_root)
            save_path = rel if not rel.startswith("..") else path
        except ValueError:
            save_path = path
        self._current_persona["avatar"] = save_path
        pix = make_round_pixmap(path, 120)
        self.rp_av_btn.setIcon(QIcon(pix))
        self.rp_av_btn.setIconSize(pix.size())
        self.rp_av_btn.setText("")
        self.rp_av_btn.setStyleSheet(f"""
            QPushButton{{background:transparent;border:2px solid {self.c['border_accent']};border-radius:60px;}}
            QPushButton:hover{{border-color:{self.c['accent']};}}
        """)

    def _collect_persona_data(self) -> dict:
        """Read all right-panel fields into a dict."""
        tools_display = self.rp_tools_combo.currentText()
        tools_profile_name = tools_display.split(" (")[0] if tools_display != "— none —" else ""
        return {
            "name": self.rp_name.text().strip(),
            "tagline": self.rp_tagline.text().strip(),
            "avatar": self._current_persona.get("avatar", ""),
            "system_prompt": self.rp_prompt.toPlainText().strip(),
            "tools_profile": tools_profile_name,
            "tts_voice": self.rp_voice.currentText(),
            "tts_speed": self.rp_speed.value() / 100.0,
            "tts_pitch": self.rp_pitch.value() / 100.0,
            "tts_volume": self.rp_vol.value() / 100.0,
            "description": self.rp_desc.toPlainText().strip(),
            "protected": self._current_persona.get("protected", False),
        }

    def _save_persona(self):
        from core.personas import save_persona
        if not self._current_path:
            return
        data = self._collect_persona_data()
        save_persona(self._current_path, data)
        self._current_persona = data
        self._current_persona["_file"] = self._current_path
        self._load_personas()
        print(f"[PERSONA] Saved: {data['name']}", flush=True)

    def _activate(self):
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        # Apply tool profile first
        if data.get("tools_profile"):
            from core.tool_profiles import list_profiles
            for p in list_profiles():
                if p.get("name") == data["tools_profile"]:
                    enabled_set = set(p.get("enabled", []))
                    all_tools = list(self.agent.registry._tools.keys())
                    disabled = [t for t in all_tools if t not in enabled_set]
                    self.agent.registry.set_disabled(disabled)
                    break
        self.agent.apply_persona(data)
        w = self
        while w and not hasattr(w, 'persona_applied'):
            w = w.parent()
        if w:
            w.persona_applied.emit(data["name"], data.get("avatar") or "")
        print(f"[PERSONA] Activated from settings: {data['name']}", flush=True)

    def _duplicate(self):
        from core.personas import save_persona, PERSONAS_DIR, list_personas
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        data["name"] = data["name"] + " (copy)"
        data["protected"] = False
        from core.personas import fname_from_name
        fname = fname_from_name(data["name"])
        new_path = os.path.join(PERSONAS_DIR, fname)
        save_persona(new_path, data)
        self._load_personas()

    def _export(self):
        from core.personas import save_persona
        if not self._current_persona:
            return
        data = self._collect_persona_data()
        name = data.get("name", "persona")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Persona", f"{name}.json", "JSON files (*.json)"
        )
        if path:
            save_persona(path, data)
            QMessageBox.information(self, "Exported", f"Persona exported to:\n{path}")

    def _delete(self):
        if not self._current_path or not self._current_persona:
            return
        if self._current_persona.get("protected", False):
            QMessageBox.warning(self, "Protected", "This persona cannot be deleted.")
            return
        name = self._current_persona.get("name", "this persona")
        reply = QMessageBox.question(
            self, "Delete Persona",
            f"Delete '{name}'? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            os.remove(self._current_path)
            self._current_path = None
            self._current_persona = None
            self._load_personas()
            self._show_placeholder()

    def _new_persona(self):
        from core.personas import save_persona, PERSONAS_DIR, fname_from_name
        name, ok = QInputDialog.getText(self, "New Persona", "Persona name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        fname = fname_from_name(name)
        new_path = os.path.join(PERSONAS_DIR, fname)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Exists", f"A persona named '{name}' already exists.")
            return
        data = {
            "name": name,
            "tagline": "",
            "avatar": "",
            "system_prompt": "",
            "tools_profile": "",
            "tts_voice": config.VOICEBOX_PROFILE if getattr(config, 'TTS_BACKEND', 'kokoro') == 'voicebox' else config.TTS_VOICE,
            "tts_speed": 1.0,
            "tts_pitch": 1.0,
            "tts_volume": 1.0,
            "description": "",
            "protected": False,
        }
        save_persona(new_path, data)
        self._load_personas()
        # Auto-select the new persona
        for p in self._personas:
            if p["_file"] == new_path:
                self._select_persona(p)
                break

    def _test_tts(self):
        if not self.agent.tts:
            return
        tts = self.agent.tts
        voice = self.rp_voice.currentText()
        # Backend-aware voice assignment
        if hasattr(tts, 'set_profile'):
            tts.set_profile(voice)
        elif hasattr(tts, 'set_voice'):
            tts.set_voice(voice)
            tts.speed = self.rp_speed.value() / 100.0
            tts.pitch = self.rp_pitch.value() / 100.0
            tts.volume = self.rp_vol.value() / 100.0
        tts.speak("Persona voice test.", blocking=False)

    def _fetch_voices(self) -> list:
        fallback = ["af_bella", "af_sarah", "af_nicole", "af_sky",
                    "am_adam", "am_michael", "bf_emma", "bf_isabella", "bf_lily"]
        try:
            backend = getattr(config, "TTS_BACKEND", "kokoro")
            if backend == "voicebox":
                from tts.loader import get_tts_backend
                return get_tts_backend().list_voices() or fallback
            else:
                import urllib.request, json
                host = config.TTS_HOST.rstrip("/")
                with urllib.request.urlopen(f"{host}/v1/audio/voices", timeout=3) as r:
                    data = json.loads(r.read())
                    voices = sorted(data.get("voices", []))
                    return voices if voices else fallback
        except Exception:
            return fallback
        
        
class AboutTab(QWidget):
    def __init__(self, c: dict, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        # ── Branding ──────────────────────────────────────────────────────────
        name_label = QLabel("Lumina AI")
        name_label.setAlignment(Qt.AlignCenter)
        name_label.setStyleSheet(f"color:{c['accent']};font-size:39px;font-weight:bold;background:transparent;")
        layout.addWidget(name_label)

        by_label = QLabel("by: Jason 'BINO' Malik · Mo Thugs South · © 2026")
        by_label.setAlignment(Qt.AlignCenter)
        by_label.setStyleSheet(f"color:{c['text_primary']};font-size:19px;background:transparent;")
        layout.addWidget(by_label)

        ver_label = QLabel("v0.1.9-beta.1")
        ver_label.setAlignment(Qt.AlignCenter)
        ver_label.setStyleSheet(f"color:{c['text_primary']};font-size:16px;background:transparent;")
        layout.addWidget(ver_label)

        layout.addSpacing(8)

        # ── Divider ───────────────────────────────────────────────────────────
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{c['border']};")
        layout.addWidget(line)

        layout.addSpacing(8)

        # ── Support text ─────────────────────────────────────────────────────
        support_label = QLabel(
            "For support, feature requests, or to connect with the community,\n"
            "visit the links below."
        )
        support_label.setAlignment(Qt.AlignCenter)
        support_label.setWordWrap(True)
        support_label.setStyleSheet(f"color:{c['text_primary']};font-size:11px;background:transparent;")
        layout.addWidget(support_label)

        layout.addSpacing(8)

        # ── Contact info ─────────────────────────────────────────────────────
        contact_label = QLabel("Contact: bino5150@gmail.com")   # ← swap with real address
        contact_label.setAlignment(Qt.AlignCenter)
        contact_label.setStyleSheet(f"color:{c['text_primary']};font-size:10px;background:transparent;")
        layout.addWidget(contact_label)

        layout.addSpacing(12)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_style = (
            f"QPushButton {{"
            f"  background:{c['bg_card']};"
            f"  color:{c['text_primary']};"
            f"  border:1px solid {c['border']};"
            f"  border-radius:6px;"
            f"  padding:8px 16px;"
            f"  font-size:11px;"
            f"}}"
            f"QPushButton:hover {{"
            f"  background:{c['accent']};"
            f"  color:#000000;"
            f"  border:1px solid {c['accent']};"
            f"}}"
        )

        gh_btn = QPushButton("⬡  GitHub")
        gh_btn.setStyleSheet(btn_style)
        gh_btn.setEnabled(True)
        gh_btn.clicked.connect(
            lambda: __import__("webbrowser").open("https://github.com/Bino5150/lumina")
        )

        discord_btn = QPushButton("◈  Discord")
        discord_btn.setStyleSheet(btn_style)
        discord_btn.setEnabled(True)
        discord_btn.clicked.connect(
            lambda: __import__("webbrowser").open("https://discord.gg/RUWsFbnk")
        )

        linkedin_btn = QPushButton("in  LinkedIn")
        linkedin_btn.setStyleSheet(btn_style)
        linkedin_btn.clicked.connect(
            lambda: __import__("webbrowser").open("https://www.linkedin.com/in/jason-malik-a97b07412/")  # ← swap handle
        )
        layout.addWidget(linkedin_btn)

        layout.addStretch()

# ── Main Settings Panel ────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    persona_applied = Signal(str, str)  # (agent_name, avatar_path)
    def __init__(self, agent, colors: dict, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.colors = colors
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{
                background: {self.colors['bg_deep']};
                border: none;
                border-top: 1px solid {self.colors['border']};
            }}
            QTabBar::tab {{
                background: {self.colors['bg_panel']};
                color: {self.colors['text_muted']};
                border: none;
                border-bottom: 2px solid transparent;
                padding: 10px 20px;
                font-size: 12px;
                font-family: 'JetBrains Mono', monospace;
            }}
            QTabBar::tab:selected {{
                color: {self.colors['accent']};
                border-bottom: 2px solid {self.colors['accent']};
                background: {self.colors['bg_deep']};
            }}
            QTabBar::tab:hover {{
                color: {self.colors['text_primary']};
                background: {self.colors['bg_card']};
            }}
        """)

        c = self.colors
        tabs.addTab(GeneralTab(self.agent, c),      "⚙  General")
        tabs.addTab(UserProfileTab(self.agent, c),  "👤  User Profile")
        self.personas_tab = PersonasTab(self.agent, c)
        self.tts_tab = TTSTab(self.agent, c)
        tabs.addTab(self.personas_tab,              "🎭  Personas")
        tabs.addTab(MemoryTab(self.agent, c),       "🧠  Memory")
        tabs.addTab(KnowledgeTab(self.agent, c),    "📚  Knowledge")
        tabs.addTab(ToolsTab(self.agent, c),        "🔧  Tools")
        tabs.addTab(self.tts_tab,                   "🔊  TTS")
        self.tts_tab.backend_changed.connect(self.personas_tab.refresh_voices)
        tabs.addTab(AboutTab(c),                    "✨  About")

        layout.addWidget(tabs)
