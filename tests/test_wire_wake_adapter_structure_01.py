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

    def test_startup_call_site_gates_on_the_pref_gate(self):
        """G1 guard (Sonnet): the startup call site must CONJUNCT the
        _wire_wake_enabled() gate. Replacing it with ``and True`` (default-on
        at startup) passed every R2 test — the pins covered the gate function,
        not the call site."""
        import re

        text = MAIN_WINDOW.read_text(encoding="utf-8")
        match = re.search(
            r"not os\.environ\.get\(\"LUMINA_TESTING\"\)\s*\n\s*and\s+"
            r"self\._wire_wake_enabled\(\)",
            text,
        )
        assert match, (
            "startup call site no longer gates on _wire_wake_enabled() "
            "(G1: default-on at startup reintroduced)"
        )

    def test_no_new_module_routes_claim_outside_the_reviewed_surface(self):
        """NEWF guard (Sonnet): the R2 claim-caller scan covered tools/*.py
        only; a new core/ module wrapping claim_wake would pass 59/59. The
        whole product tree is scanned: only the listener (defines the
        owner-only action), the client (transport), and the window wiring
        (exactly-once, checked by its own test) may reference claim."""
        allowed = {
            "wire_wake_listener.py",   # defines the owner-only action
            "aivengers_wire.py",       # defines the client transport method
            "main_window.py",          # exactly-once checked by its own test
        }
        offenders = []
        for base in (REPO_ROOT / "core", REPO_ROOT / "comms", REPO_ROOT / "ui"):
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if path.name in allowed:
                    continue
                text = path.read_text(errors="replace")
                if "claim_wake" in text or "WireWakeListener(" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert not offenders, (
            "new module routes claim/listener outside the reviewed surface: "
            f"{offenders}"
        )


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
        """R3/R2-2 guard: the backoff reset must live inside the frame loop
        AND must be gated on a NON-subscribed frame — the ``subscribed`` ack
        is not a valid post-subscribe frame (Sonnet P1: ack-then-fail must
        keep backing off)."""
        text = LISTENER.read_text(encoding="utf-8")
        loop_idx = text.index("for event in self._client.watch(")
        reset_idx = text.index("backoff = RECONNECT_MIN_SECONDS", loop_idx)
        assert reset_idx > loop_idx, (
            "backoff reset must happen after the watch loop starts consuming"
        )
        # the reset must sit behind the got_real_frame latch AND the
        # non-subscribed condition (the R2 shape reset on the ack itself)
        got_idx = text.index("got_real_frame = True", loop_idx)
        cond_idx = text.index('kind != "subscribed"', loop_idx)
        assert cond_idx < got_idx < reset_idx, (
            "backoff reset must be gated on a non-subscribed real frame"
        )

    def test_catchup_runs_on_connect_only(self):
        """R3/R2-4 guard: catch-up is gated behind the _catchup_needed latch,
        the latch is cleared only AFTER the walk, and error-driven reconnects
        re-arm it (the relay replays only the oldest ~32 unacked pending
        wakes per subscription)."""
        text = LISTENER.read_text(encoding="utf-8")
        assert "self._catchup_needed.clear()" in text
        assert "self._catchup_needed.set()" in text
        # clear must FOLLOW the walk (R2 cleared before it — one transient
        # failure lost the catch-up for the life of the listener)
        walk_idx = text.index("self._emit_catchup(session[\"session_id\"])")
        clear_idx = text.index("self._catchup_needed.clear()", walk_idx)
        assert clear_idx > walk_idx, "latch cleared before the walk (R2-4)"
        # the error handlers must re-arm the latch
        except_idx = text.index("except WireError as exc:", walk_idx)
        rearm_idx = text.index("self._catchup_needed.set()", except_idx)
        assert rearm_idx > except_idx, (
            "error-driven reconnect must re-arm the catch-up latch"
        )

    def test_catchup_walks_live_states_only(self):
        text = LISTENER.read_text(encoding="utf-8")
        assert '_CATCHUP_STATES = ("pending", "delivered", "claimed")' in text
