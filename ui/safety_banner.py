"""Current C1 status outside conversation history, with persistent navigation."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from core import suspension_runtime as R


class SafetyBanner(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.controls_btn = QPushButton("Open safety controls")
        self.controls_btn.setCursor(Qt.PointingHandCursor)
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.controls_btn)

    def refresh(self, status):
        warning = True
        if status.state is R.State.ACTIVE:
            if status.active_incidents:
                text = f"Runaway suspension: {status.active_incidents} scope(s) are suspended. Review safety controls."
            else:
                text = "Safety protection is active; nothing is suspended."
                warning = False
        else:
            text = "Runaway suspension isn't ready for this data folder. Work stays paused. Open safety controls to set up or recover protection."
        if status.local_stops:
            text += " Stopped in this process only (global suspension unconfirmed). Review safety controls."
            warning = True
        self.status_label.setText(text)
        self.status_label.setStyleSheet("color: #ef9a9a;" if warning else "color: #81c784;")
