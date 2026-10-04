"""About tab community link button: presence, order, and destination.

The About tab is public product surface, but nothing in it was covered by a
test -- the link buttons were only ever verified by opening Settings and
looking. This pins the one link that has a real failure mode nobody would
notice: a typo'd or restructured subreddit URL that still renders a
perfectly ordinary-looking button.

Asserts the button exists, sits between GitHub and Discord in the layout
(it is the community surface, not an afterthought parked at the bottom),
and opens exactly the intended URL with nothing appended or dropped.

tests.yml runs this file with LUMINA_REQUIRE_QT=1 (fail, never skip).
"""
import os

import pytest

if os.environ.get("LUMINA_REQUIRE_QT") == "1":
    import PySide6  # noqa: F401 -- the Qt guard job must fail, never skip
else:
    pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from ui.main_window import COLORS  # noqa: E402 -- the real palette every tab is built with
from ui.settings.about_tab import AboutTab  # noqa: E402

SUBREDDIT_URL = "https://www.reddit.com/r/AgentsInteractive/"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def tab(qapp):
    return AboutTab(COLORS)


def _buttons(tab):
    return [w for w in tab.findChildren(QPushButton) if w.isVisible() or w.text()]


def _by_text(tab, needle):
    matches = [b for b in _buttons(tab) if needle in b.text()]
    assert len(matches) == 1, f"expected exactly one button containing {needle!r}, got {len(matches)}"
    return matches[0]


def test_subreddit_button_is_present_and_enabled(tab):
    btn = _by_text(tab, "r/AgentsInteractive")
    assert btn.isEnabled(), "the subreddit link button must be clickable"


def test_subreddit_button_sits_between_github_and_discord(tab):
    buttons = _buttons(tab)
    labels = [b.text() for b in buttons]
    gh = next(i for i, t in enumerate(labels) if "GitHub" in t)
    sub = next(i for i, t in enumerate(labels) if "r/AgentsInteractive" in t)
    dis = next(i for i, t in enumerate(labels) if "Discord" in t)
    assert gh < sub < dis, f"expected GitHub < subreddit < Discord, got {labels}"


def test_subreddit_button_opens_exactly_the_subreddit_url(tab, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    _by_text(tab, "r/AgentsInteractive").click()

    assert opened == [SUBREDDIT_URL]


def test_subreddit_button_matches_sibling_button_styling(tab):
    """It must look like a sibling link button, not a differently-shaped control."""
    sub = _by_text(tab, "r/AgentsInteractive")
    gh = _by_text(tab, "GitHub")
    assert sub.styleSheet() == gh.styleSheet()