"""Focused regression for Settings > Skills (SKILLS-SETTINGS-UI-01).

Real-Qt tests, same convention as test_settings_scheduled_tasks_tab.py /
test_settings_tools_tab_layout.py: offscreen QPA platform, a session-scoped
QApplication from conftest.py, and a hermetic per-test skills environment
(config.DB_PATH and config.BASE_DIR both monkeypatched to tmp_path, so
init_skills_db()/write_skill()/list_skills()/load_skill() -- the exact
functions the tab calls -- read and write only inside the test sandbox).

Every inventory-truth assertion here seeds data through core.skills.write_skill(),
the same function the save_skill tool calls, never a hand-rolled INSERT --
that's what makes "the UI reflects machine/source truth" a real claim rather
than a coincidence of two independently-hardcoded lists agreeing.
"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView

import config
from ui.main_window import COLORS
from ui.settings.skills_tab import SkillsTab


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Hermetic skills environment: fresh DB file + fresh skills/ directory,
    both re-read live by core.skills on every call (config.DB_PATH and
    config.BASE_DIR are read fresh, never captured at import time -- see
    core/db.py's connect() and core/skills.py's _skills_dir())."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "lumina.db"))
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def tab(env):
    QApplication.instance() or QApplication([])
    agent = object()  # SkillsTab never touches .agent; a real Agent is not needed
    return SkillsTab(agent, COLORS)


def _seed(name="scrape-a-webpage", description="Scrape and summarize a page",
          content="# Skill: scrape-a-webpage\n\nStep 1. Fetch.\nStep 2. Summarize.\n"):
    from core.skills import write_skill
    return write_skill(name, description, content)


# ── Inventory truth ──────────────────────────────────────────────────────────

def test_canonical_skill_appears_with_real_metadata(tab, env):
    _seed()
    tab._load()
    assert tab.table.rowCount() == 1
    assert tab.table.item(0, 0).text() == "scrape-a-webpage"
    assert tab.table.item(0, 1).text() == "Available"
    assert "skill(s)" in tab.status_lbl.text()


def test_ui_does_not_rely_on_a_stale_hardcoded_list(tab, env):
    """The whole point of this tab: it must reflect whatever is actually in
    core.skills right now, not a list baked in at construction time."""
    tab._load()
    assert tab.table.rowCount() == 0
    _seed(name="newly-added-skill", description="added after the tab was built")
    tab._load()
    assert tab.table.rowCount() == 1
    assert tab.table.item(0, 0).text() == "newly-added-skill"


def test_refresh_discovers_a_newly_added_canonical_skill(tab, env):
    tab._load()
    assert tab.table.rowCount() == 0
    _seed(name="second-skill", description="discovered via refresh")
    tab._load()  # same call the Refresh button's clicked signal invokes
    names = {tab.table.item(i, 0).text() for i in range(tab.table.rowCount())}
    assert "second-skill" in names


def test_removed_skill_does_not_leave_stale_selection_pointing_at_another_row(tab, env):
    _seed(name="alpha", description="first")
    _seed(name="beta", description="second")
    tab._load()
    # Select "alpha" specifically (rows are ordered by name -- alpha < beta).
    row_of_alpha = next(i for i in range(tab.table.rowCount()) if tab.table.item(i, 0).text() == "alpha")
    tab.table.selectRow(row_of_alpha)
    assert tab._selected_name == "alpha"

    # Remove alpha's backing DB row by pointing at a fresh empty DB (simulates
    # the skill disappearing from the canonical inventory between refreshes)
    # while leaving "beta" in place at what is now row 0.
    from core.skills import get_db
    conn = get_db()
    conn.execute("DELETE FROM skills WHERE name=?", ("alpha",))
    conn.commit()
    conn.close()

    tab._load()
    assert tab.table.rowCount() == 1
    assert tab.table.item(0, 0).text() == "beta"
    # Must NOT silently show beta's content under alpha's old selection.
    assert tab._selected_name is None
    assert tab.detail_name.text() == ""
    assert tab.content_view.toPlainText() == ""


# ── Full-content truth ───────────────────────────────────────────────────────

def test_exact_multiline_content_is_readable_and_intact(tab, env):
    body = "# Skill: x\n\nLine one.\nLine two.\n\n## Pitfalls\n- gotcha one\n- gotcha two\n"
    _seed(name="x", description="multiline test", content=body)
    tab._load()
    tab.table.selectRow(0)
    assert tab.content_view.toPlainText() == body


def test_markdown_and_html_like_content_renders_literally_not_interpreted(tab, env):
    """Mutation M2 target: rendering through setHtml()/setMarkdown() instead
    of setPlainText() must turn this RED."""
    hostile = "# Skill: hostile-content\n**bold**\n<b>not markup</b>\n[click me](file:///etc/passwd)\n<script>alert(1)</script>\n"
    _seed(name="hostile-content", description="d", content=hostile)
    tab._load()
    tab.table.selectRow(0)

    assert tab.content_view.toPlainText() == hostile
    html = tab.content_view.document().toHtml()
    # A literal plain-text document escapes angle brackets in its HTML
    # serialization; an interpreted document would contain a real <script>
    # or <b> element instead of the escaped source text.
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;not markup&lt;/b&gt;" in html


def test_hostile_metadata_forces_plain_text_label_rendering(tab, env):
    """Mutation target: dropping setTextFormat(Qt.PlainText) on the QLabel
    detail fields must turn this RED (QLabel defaults to Qt::AutoText,
    which auto-detects and renders HTML-looking strings as rich text)."""
    _seed(name="<b>evil</b>", description="<script>alert(1)</script>")
    tab._load()
    tab.table.selectRow(0)
    assert tab.detail_name.textFormat() == Qt.PlainText
    assert tab.detail_meta.textFormat() == Qt.PlainText
    assert tab.detail_name.text() == "<b>evil</b>"
    assert "<script>alert(1)</script>" in tab.detail_meta.text()


def test_malformed_or_missing_backing_content_is_truthful_not_silent(tab, env):
    result = _seed(name="ghost", description="db row with no file")
    os.remove(result["path"])
    tab._load()
    assert tab.table.item(0, 1).text() == "Missing source"
    tab.table.selectRow(0)
    assert "unavailable" in tab.content_view.toPlainText().lower()
    assert "Missing source" in tab.detail_meta.text()


# ── Read-only boundary ────────────────────────────────────────────────────────

def test_table_rejects_in_place_editing(tab, env):
    """Mutation M4 target: removing NoEditTriggers must turn this RED."""
    assert tab.table.editTriggers() == QAbstractItemView.NoEditTriggers


def test_content_viewer_is_read_only(tab, env):
    """Mutation M4 target: setReadOnly(False) must turn this RED."""
    assert tab.content_view.isReadOnly() is True


def test_no_mutation_controls_are_exposed(tab, env):
    from PySide6.QtWidgets import QPushButton
    labels = {b.text().strip().lower() for b in tab.findChildren(QPushButton)}
    forbidden = {"save", "delete", "edit", "rename", "import", "delete selected", "save skill"}
    assert not (labels & forbidden)


def test_opening_selecting_and_refreshing_never_mutate_storage(tab, env):
    result = _seed(name="untouched", description="must not change")
    with open(result["path"], "r", encoding="utf-8") as f:
        original_bytes = f.read()
    original_mtime = os.path.getmtime(result["path"])

    from core.skills import get_db
    conn = get_db()
    row_count_before = conn.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"]
    conn.close()

    tab._load()
    tab.table.selectRow(0)
    tab._load()  # Refresh

    with open(result["path"], "r", encoding="utf-8") as f:
        assert f.read() == original_bytes
    assert os.path.getmtime(result["path"]) == original_mtime

    conn = get_db()
    row_count_after = conn.execute("SELECT COUNT(*) AS c FROM skills").fetchone()["c"]
    conn.close()
    assert row_count_after == row_count_before


# ── Empty / degraded states ───────────────────────────────────────────────────

def test_empty_inventory_state(tab, env):
    tab._load()
    assert tab.table.rowCount() == 0
    assert tab.status_lbl.text() == "No skills saved yet."


def test_database_unavailable_is_reported_distinctly_from_empty(tab, env, monkeypatch):
    """Mutation M3-adjacent: collapsing this into the same message as the
    empty-inventory case would hide a real index failure from the owner."""
    import core.skills as skills_mod

    def _raise():
        raise RuntimeError("simulated index failure")

    monkeypatch.setattr(skills_mod, "init_skills_db", _raise)
    tab._load()
    assert "unavailable" in tab.status_lbl.text().lower()
    assert tab.status_lbl.text() != "No skills saved yet."


def test_filter_narrows_by_name_and_description(tab, env):
    _seed(name="alpha-task", description="handles alpha work")
    _seed(name="beta-task", description="unrelated")
    tab._load()
    tab.filter_le.setText("alpha")
    names = {tab.table.item(i, 0).text() for i in range(tab.table.rowCount())}
    assert names == {"alpha-task"}


def test_scope_field_is_a_single_honest_label_not_fabricated_categories(tab, env):
    """SKILLS-SETTINGS-UI-01 source-vet finding: core/skills.py has no scope
    column and no built-in/user/project distinction anywhere in its schema.
    This asserts the detail pane says something true and constant rather
    than inventing a category the data can't actually support."""
    _seed(name="s1", description="d1")
    tab._load()
    tab.table.selectRow(0)
    assert "Scope: Persistent skill" in tab.detail_meta.text()
