from PySide6.QtWidgets import QWidget, QVBoxLayout, QTabWidget
from PySide6.QtCore import Signal

from .general_tab import GeneralTab
from .user_profile_tab import UserProfileTab
from .memory_tab import MemoryTab
from .knowledge_tab import KnowledgeTab
from .tools_tab import ToolsTab
from .tts_tab import TTSTab
from .communications_tab import CommunicationsTab
from .personas_tab import PersonasTab
from .scheduled_tasks_tab import ScheduledTasksTab
from .about_tab import AboutTab
from .coming_soon_tab import ComingSoonTab


# ── Main Settings Panel ────────────────────────────────────────────────────────

class SettingsPanel(QWidget):
    persona_applied = Signal(str, str)  # (agent_name, avatar_path)
    backend_connection_changed = Signal()  # UI-TRUST-01B: relayed from GeneralTab
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
        self.general_tab = GeneralTab(self.agent, c)
        tabs.addTab(self.general_tab,      "⚙  General")
        tabs.addTab(UserProfileTab(self.agent, c),  "👤  User Profile")
        self.personas_tab = PersonasTab(self.agent, c)
        self.tts_tab = TTSTab(self.agent, c)
        tabs.addTab(self.personas_tab,              "🎭  Personas")
        tabs.addTab(CommunicationsTab(self.agent, c), "📡  Communications")
        tabs.addTab(MemoryTab(self.agent, c),       "🧠  Memory")
        tabs.addTab(KnowledgeTab(self.agent, c),    "📚  Knowledge")
        tabs.addTab(ToolsTab(self.agent, c),        "🔧  Tools")
        tabs.addTab(ScheduledTasksTab(self.agent, c), "🗓  Scheduled Tasks")
        tabs.addTab(self.tts_tab,                   "🔊  TTS")
        self.tts_tab.backend_changed.connect(self.personas_tab.refresh_voices)
        self.general_tab.backend_connection_changed.connect(
            self.backend_connection_changed
        )
        tabs.addTab(ComingSoonTab("Image Generation", "Native inline image generation — local and cloud backends. Tracked as MB-21.", c), "🎨  Image Gen")
        tabs.addTab(ComingSoonTab("Oracle", "A performance-oriented, full featured local inference server with integrated dashboard.", c), "🔮  Oracle")
        tabs.addTab(AboutTab(c),                    "✨  About")

        layout.addWidget(tabs)
