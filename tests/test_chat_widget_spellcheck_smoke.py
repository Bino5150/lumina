"""
Smoke test for the spellcheck highlighter + right-click suggestions wired
into SmartInput.

Queries formatting via block.layout().formats(), not QTextCursor.charFormat()
-- the latter reflects the document's actual character formatting, not
formatting applied by a QSyntaxHighlighter (a separate paint-time layer).
This cost real debugging time to work out; keeping the comment so this
doesn't get "corrected" back to the wrong API later.

Run headless from repo root:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_chat_widget_spellcheck_smoke.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail}")


def main():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QTextCharFormat, QTextCursor
    app = QApplication.instance() or QApplication(sys.argv)

    from ui.chat_widget import SmartInput
    from ui.main_window import COLORS

    inp = SmartInput(COLORS)
    check("SmartInput instantiates with the spellcheck highlighter attached",
          hasattr(inp, "_spell_highlighter"))

    text = "MemPalace and TurboQuant correclty spelled"
    inp.setPlainText(text)
    app.processEvents()

    block = inp.document().firstBlock()
    formats = block.layout().formats()
    underlined = [text[fr.start:fr.start + fr.length] for fr in formats
                  if fr.format.underlineStyle() == QTextCharFormat.UnderlineStyle.SpellCheckUnderline]

    check("MemPalace is underlined (not a real word)", "MemPalace" in underlined, f"underlined={underlined}")
    check("TurboQuant is underlined (not a real word)", "TurboQuant" in underlined, f"underlined={underlined}")
    check("correclty is underlined (misspelled)", "correclty" in underlined, f"underlined={underlined}")
    check("and is NOT underlined (correctly spelled)", "and" not in underlined)
    check("spelled is NOT underlined (correctly spelled)", "spelled" not in underlined)

    idx = text.index("correclty")
    cur = QTextCursor(inp.document())
    cur.setPosition(idx + 2)
    cur.select(QTextCursor.SelectionType.WordUnderCursor)
    check("word-under-cursor extraction finds the right word",
          cur.selectedText() == "correclty", f"got={cur.selectedText()!r}")

    check("SmartInput has the contextMenuEvent override", "contextMenuEvent" in SmartInput.__dict__)
    check("SmartInput has _apply_suggestion", hasattr(inp, "_apply_suggestion"))

    inp.setPlainText("this has a typo")
    idx2 = inp.toPlainText().index("typo")
    cur2 = QTextCursor(inp.document())
    cur2.setPosition(idx2)
    cur2.setPosition(idx2 + len("typo"), QTextCursor.MoveMode.KeepAnchor)
    inp._apply_suggestion(cur2, "correction")
    check("_apply_suggestion replaces the selected word",
          "correction" in inp.toPlainText(), f"got={inp.toPlainText()!r}")

    print(f"\n{'='*60}")
    print(f"PASS: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        print("\nFailed checks:")
        for f in FAIL:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nAll checks passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
