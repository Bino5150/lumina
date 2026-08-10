from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel
from PySide6.QtCore import Qt


class ComingSoonTab(QWidget):
    """Reusable placeholder tab for features that are planned but not yet
    built. Disabled by design — nothing on this tab does anything yet."""

    def __init__(self, title: str, subtitle: str, c: dict, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(10)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet(f"color:{c['text_muted']};font-size:22px;font-weight:bold;background:transparent;")
        layout.addWidget(title_label)

        badge = QLabel("COMING SOON")
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"color:{c['accent_dim']};font-size:11px;letter-spacing:2px;font-weight:bold;background:transparent;")
        layout.addWidget(badge)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setWordWrap(True)
        subtitle_label.setFixedWidth(360)
        subtitle_label.setStyleSheet(f"color:{c['text_muted']};font-size:11px;background:transparent;")
        layout.addWidget(subtitle_label)
