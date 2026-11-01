"""WIRE-WAKE-01 W1 — Qt-free structural guards for the native wake adapter.

R2/B1: these pins need NEITHER PySide6 NOR the aivengers relay source, so
they run in the ordinary CI ``test`` job and block there. The behavioral
Qt/relay tests live in ``test_wire_wake_adapter_01.py`` (importorskip +
qt-security-guards step).

Each guard is a source/AST pin against the R2 findings:

- zero-inference: the listener imports no execution path — including LAZY
  imports (``__import__``, ``importlib``), which a plain import-list scan
  misses (review mutant M7a/M7b);
- claim is unreachable from model-facing tools (no tool wraps the listener's
  claim path; MainWindow references it exactly once — the panel signal
  connection; review mutant M6 auto-claim);
- default-OFF activation: the startup gate reads the persisted pref with
  default False and the only env override is the explicit canary variable
  (review mutant M8 / B3);
- panel renders Wire-sourced text as plain text (no rich-text interpretation
  of peer content);
- the listener never touches seat tokens (the client owns token loading).

These are structural pins, not behavioral proofs — the behavioral proofs are
the relay/Qt tests. Their job is to fail in ordinary CI if any of these
properties regresses.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LISTENER = REPO_ROOT / "comms" / "wire_wake_listener.py"
PANEL = REPO_ROOT / "ui" / "wire_panel.py"
MAIN_WINDOW = REPO_ROOT / "ui" / "main_window.py"
TOOLS_DIR = REPO_ROOT / "tools"


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def _lazy_import_text(path: Path) -> list[str]:
    """Text-level scan for lazy-import escapes (M7a/M7b class).

    AST import-lists miss function-local ``from core import agent`` only if
    they are dynamic; function-local static imports DO appear in ast.walk.
    The truly dynamic forms are ``__import__(...)`` and ``importlib`` —
    scanned at text level here because they are string-shaped.
    """
    text = path.read_text(encoding="utf-8")
    hits: list[str] = []
    if "__import__" in text:
        hits.append("__import__")
    if "importlib" in text:
        hits.append("importlib")
    return hits


class TestZeroInference:
    def test_listener_imports_no_execution_path(self):
        banned = (
            "core.agent", "core.turn_recovery", "core.suspension",
            "core.emergency_stop", "tools.guardrails", "tools.terminal",
            "core.task_queue", "core.backends", "core.capability_router",
        )
        imported = _imports_of(LISTENER)
        hits = [
            name for name in imported
            if any(name == b or name.startswith(b + ".") for b in banned)
        ]
        assert not hits, f"listener imports execution paths: {hits}"

    def test_listener_has_no_lazy_import_escape(self):
        hits = _lazy_import_text(LISTENER)
        assert not hits, f"listener uses dynamic import escapes: {hits}"

    def test_listener_import_allowlist(self):
        """Pin the listener's full import surface; new imports must be
        deliberate (and re-reviewed), not silent."""
        allowed = {
            "__future__", "os", "re", "socket", "threading", "time", "uuid",
            "typing", "PySide6.QtCore", "tools.aivengers_wire",
        }
        imported = set(_imports_of(LISTENER))
        unexpected = imported - allowed
        assert not unexpected, (
            f"listener gained imports outside the reviewed allow-list: "
            f"{sorted(unexpected)}"
        )


class TestClaimUnreachableFromModelPaths:
    def test_no_tool_wraps_the_listener_claim_path(self):
        offenders = []
        for path in TOOLS_DIR.glob("*.py"):
            text = path.read_text(errors="replace")
            if path.name == "aivengers_wire.py":
                continue  # defines the client method; registers no tool
            if "wire_wake_listener" in text or "claim_wake" in text:
                offenders.append(path.name)
        assert not offenders, f"model-facing tool path reaches claim: {offenders}"

    def test_mainwindow_references_claim_wake_exactly_once(self):
        """M6 guard: the ONLY claim_wake reference in the window wiring is
        the panel-signal connection. An auto-claim added to a wire slot
        would add a second reference and fail here."""
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        count = text.count("claim_wake")
        assert count == 1, (
            f"ui/main_window.py references claim_wake {count} times; the only "
            "permitted reference is the panel signal connection"
        )
        assert "self.wire_panel.claim_requested.connect(listener.claim_wake)" in text

    def test_mainwindow_wire_slots_do_not_execute_actions(self):
        """The wire event slots may only update the panel and the admission
        note — never call listener actions or agent/turn machinery."""
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        for slot in ("_on_wire_wake_event", "_on_wire_session_state"):
            start = text.index(f"def {slot}")
            end = text.index("\n    def ", start + 1)
            body = text[start:end]
            for banned in ("claim_wake", "ack_wake", "release_task",
                           "turn_recovery", "agent.chat", "begin_tool_dispatch"):
                assert banned not in body, f"{slot} references {banned}"


class TestDefaultOffActivation:
    def test_startup_gate_defaults_to_off(self):
        """B3/M8 guard: the startup gate must read the pref with default
        False (absent == off). Flipping the default or ignoring the
        kill-switch fails here in plain CI."""
        text = MAIN_WINDOW.read_text(encoding="utf-8")
        assert 'get("wire_wake_listener", False)' in text, (
            "wire_wake_listener pref must default to False (default-OFF law)"
        )
        assert 'os.environ.get("LUMINA_WIRE_WAKE") == "1"' in text, (
            "the only env override must be the explicit LUMINA_WIRE_WAKE=1 canary"
        )
        assert text.count('"wire_wake_listener", True') == 0, (
            "no default-on path may exist for the wake listener"
        )

    def test_settings_toggle_persists_the_same_pref(self):
        tab = REPO_ROOT / "ui" / "settings" / "communications_tab.py"
        text = tab.read_text(encoding="utf-8")
        assert 'get("wire_wake_listener", False)' in text
        assert 'update({"wire_wake_listener": enabled})' in text


class TestPanelInjectionSafety:
    def test_panel_renders_wire_text_as_plain_text(self):
        text = PANEL.read_text(encoding="utf-8")
        assert "Qt.TextFormat.PlainText" in text
        assert "RichText" not in text
        assert "setOpenExternalLinks" not in text

    def test_panel_has_no_execution_imports(self):
        banned = ("core.agent", "core.turn_recovery", "core.suspension",
                  "core.emergency_stop", "tools.")
        imported = _imports_of(PANEL)
        hits = [
            name for name in imported
            if any(name == b or name.startswith(b + ".") for b in banned)
        ]
        assert not hits, f"panel imports execution paths: {hits}"


class TestTokenConfidentiality:
    def test_listener_never_touches_seat_tokens(self):
        """The client owns token loading; the listener must not read, hold,
        or log token material."""
        text = LISTENER.read_text(encoding="utf-8")
        for banned in ("_load_token", "token_path", "seat_dir", ".token"):
            assert banned not in text, (
                f"listener references token material directly: {banned}"
            )


class TestShutdownAndBackoffStructure:
    def test_shutdown_closes_the_watch_socket(self):
        """H2 guard: shutdown must unblock recv via the client's watch-socket
        hook, not wait for the server timeout event."""
        text = LISTENER.read_text(encoding="utf-8")
        assert "_watch_socket" in text
        assert "SHUT_RDWR" in text

    def test_backoff_resets_only_after_a_valid_frame(self):
        """B2 guard: the backoff reset must live inside the frame loop
        (after a frame arrives), not immediately after emitting listening."""
        text = LISTENER.read_text(encoding="utf-8")
        listen_idx = text.index('connection_state.emit("listening")')
        loop_idx = text.index("for event in self._client.watch(")
        reset_idx = text.index("backoff = RECONNECT_MIN_SECONDS", loop_idx)
        assert reset_idx > loop_idx, (
            "backoff reset must happen after the watch loop starts consuming"
        )
        # and the reset must sit after the got_frame latch, inside the loop
        got_idx = text.index("got_frame = True", loop_idx)
        assert got_idx < reset_idx

    def test_catchup_runs_on_connect_only(self):
        """H3 guard: catch-up is gated behind the _catchup_needed latch, and
        the routine timeout path must not set it."""
        text = LISTENER.read_text(encoding="utf-8")
        assert "self._catchup_needed.clear()" in text
        assert "self._catchup_needed.set()" in text
        # The only .set() sites are start() and _register_session().
        set_sites = [
            i for i in range(len(text))
            if text.startswith("self._catchup_needed.set()", i)
        ]
        assert len(set_sites) == 2, (
            "catch-up latch must be armed only at start and fresh registration"
        )

    def test_catchup_walks_live_states_only(self):
        text = LISTENER.read_text(encoding="utf-8")
        assert '_CATCHUP_STATES = ("pending", "delivered", "claimed")' in text
