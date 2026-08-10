"""
SpellCheckHighlighter — underlines misspelled words in a QTextEdit using
core/spellcheck.py. Fails open: if pyenchant or a system dictionary isn't
available, this becomes a no-op rather than crashing SmartInput.
"""
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor

from core.spellcheck import is_available, check_word, iter_words


class SpellCheckHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._enabled = is_available()
        self._format = QTextCharFormat()
        self._format.setUnderlineColor(QColor("#ff5555"))
        self._format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)

    def highlightBlock(self, text: str):
        if not self._enabled:
            return
        for word, start, end in iter_words(text):
            if len(word) > 1 and not check_word(word):
                self.setFormat(start, end - start, self._format)
