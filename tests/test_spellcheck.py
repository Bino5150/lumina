"""
Tests for core/spellcheck.py. Assumes libenchant + en_US are present at
the OS level (confirmed on Skynet, S55 session).
"""
from core import spellcheck as sc


def test_is_available():
    assert sc.is_available() is True


def test_check_word_correct():
    assert sc.check_word("hello") is True


def test_check_word_incorrect():
    assert sc.check_word("helo") is False


def test_check_word_empty_string_does_not_throw():
    # Regression test — enchant.Dict.check("") raises ValueError directly.
    # Caught while developing this feature, not a hypothetical edge case.
    assert sc.check_word("") is True


def test_check_word_handles_punctuation_without_throwing():
    for w in ["123", "-", "'", "   "]:
        sc.check_word(w)  # must not raise


def test_suggest_contains_the_intended_word():
    assert "hello" in [s.lower() for s in sc.suggest("helo")]


def test_suggest_capped_at_eight():
    assert len(sc.suggest("teh")) <= 8


def test_iter_words_offsets_are_self_consistent():
    text = "MemPalace and TurboQuant aren't in the dictionary yet"
    for word, start, end in sc.iter_words(text):
        assert text[start:end] == word


def test_iter_words_finds_all_word_tokens():
    text = "MemPalace and TurboQuant aren't correclty spelled"
    words = [w for w, _, _ in sc.iter_words(text)]
    assert words == ["MemPalace", "and", "TurboQuant", "aren't", "correclty", "spelled"]


def test_add_to_dictionary_calls_the_backend(monkeypatch):
    # Mocked deliberately -- calling the real add() would permanently write
    # "zzztest" into whoever runs this suite's actual personal en_US
    # dictionary on disk. Not a side effect a test run should have.
    calls = []

    class FakeDict:
        def add(self, word):
            calls.append(word)

    monkeypatch.setattr(sc, "get_dict", lambda: FakeDict())
    sc.add_to_dictionary("zzztest")
    assert calls == ["zzztest"]
