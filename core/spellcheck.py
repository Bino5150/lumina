"""
Spellcheck backend — wraps pyenchant. Isolated from ui/ so this stays
testable without a running QApplication.

Requires libenchant-2-2 plus a dictionary backend (hunspell or aspell) at
the OS level, not just pip install. Confirmed present on Skynet: aspell +
aspell-en + hunspell-en-us + enchant-2 (checked S55 session, no apt install
needed for this task).
"""
import re

_WORD_RE = re.compile(r"[A-Za-z']+")

_dict = None
_dict_error = None


def get_dict():
    """Lazily construct the enchant dictionary. Returns None if pyenchant
    or a system dictionary backend isn't available -- callers must treat
    that as 'spellcheck disabled', not crash."""
    global _dict, _dict_error
    if _dict is not None or _dict_error is not None:
        return _dict
    try:
        import enchant
        _dict = enchant.Dict("en_US")
    except Exception as e:
        _dict_error = str(e)
        _dict = None
    return _dict


def is_available() -> bool:
    return get_dict() is not None


def check_word(word: str) -> bool:
    """True if correctly spelled, spellcheck is unavailable (fail open --
    never block typing over a missing dependency), or word is empty
    (enchant.Dict.check("") raises ValueError directly -- caught during
    development, this guard is a fix, not a hypothetical)."""
    d = get_dict()
    if d is None or not word:
        return True
    return d.check(word)


def suggest(word: str) -> list:
    d = get_dict()
    if d is None:
        return []
    return d.suggest(word)[:8]


def add_to_dictionary(word: str):
    d = get_dict()
    if d is not None:
        d.add(word)


def iter_words(text: str):
    """Yield (word, start, end) for each word-like token in text."""
    for m in _WORD_RE.finditer(text):
        yield m.group(), m.start(), m.end()
