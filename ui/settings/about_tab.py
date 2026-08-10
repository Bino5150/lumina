from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QFrame
from PySide6.QtCore import Signal, QThread, Qt


class _UpdateCheckWorker(QThread):
    result_ready = Signal(str)

    def run(self):
        from tools.updates import check_for_updates
        self.result_ready.emit(check_for_updates())


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

        ver_label = QLabel("v0.2.7-beta.2")
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
        contact_label = QLabel("Bino: bino5150@gmail.com\nLumina: therealagentlumina@gmail.com")
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
        layout.addWidget(gh_btn, alignment=Qt.AlignCenter)
        layout.addWidget(discord_btn, alignment=Qt.AlignCenter)
        layout.addWidget(linkedin_btn, alignment=Qt.AlignCenter)

        layout.addSpacing(12)

        self.update_btn = QPushButton("⟳  Check for Updates")
        self.update_btn.setStyleSheet(btn_style)
        self.update_btn.clicked.connect(self._check_updates)
        layout.addWidget(self.update_btn, alignment=Qt.AlignCenter)

        self.update_status = QLabel("")
        self.update_status.setAlignment(Qt.AlignCenter)
        self.update_status.setWordWrap(True)
        self.update_status.setStyleSheet(f"color:{c['text_muted']};font-size:10px;background:transparent;")
        layout.addWidget(self.update_status)

        layout.addStretch()

    def _check_updates(self):
        self.update_btn.setEnabled(False)
        self.update_btn.setText("⟳  Checking...")
        self.update_status.setText("")
        self._update_worker = _UpdateCheckWorker()
        self._update_worker.result_ready.connect(self._on_update_result)
        self._update_worker.start()

    def _on_update_result(self, result: str):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("⟳  Check for Updates")
        self.update_status.setText(result)
