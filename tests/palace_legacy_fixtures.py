"""PALACE-GUARD-01B-3 test support: legacy-shaped privileged Palace rows.

Before 01B-3 the generic writers accepted layer 0/1 from any caller, so real
installs hold single-drawer L0/L1 closets and L0/L1 hall rows with NO admission
stamp. Several older tests need exactly that shape as a fixture (guard rules
about always-injected layers, context rendering, ...). The generic writers now
refuse layer < 2 -- deliberately, with no bypass parameter -- so those fixtures
are built the way legacy state actually came to exist: a row is written through
the real L2 path and then relayered in the database, leaving admission NULL
("ordinary/legacy/unknown provenance"). This is a fixture for OLD state; it is
not a production path and nothing in product code imports it.
"""
import sqlite3

import config
from tools import palace


def _relayer(table: str, row_id: int, layer: int) -> None:
    conn = sqlite3.connect(config.DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"UPDATE {table} SET layer=? WHERE id=?", (layer, row_id))
        conn.commit()
    finally:
        conn.close()


def legacy_privileged_closet(content, *, wing, room, layer, tags=None, **store_kwargs):
    """A pre-01B-3 palace_store(layer=0|1) result: a single-drawer closet at
    layer 0/1 with admission NULL. `room` must be fresh so the L2 closet the
    real write creates holds exactly this drawer (L0/L1 closets never merged).
    Extra keyword arguments (e.g. untrusted=...) pass straight to palace_store;
    omit `untrusted` to exercise the writer's own default."""
    assert layer in (0, 1), "legacy privileged fixtures are for layer 0/1 only"
    result = palace.palace_store(content, wing, room, 2, tags, **store_kwargs)
    conn = sqlite3.connect(config.DB_PATH)
    try:
        drawers = conn.execute(
            "SELECT COUNT(*) FROM palace_drawers WHERE closet_id=?", (result["closet_id"],)
        ).fetchone()[0]
    finally:
        conn.close()
    assert drawers == 1, "legacy privileged fixture needs a fresh room (one drawer per closet)"
    _relayer("palace_closets", result["closet_id"], layer)
    return result


def legacy_privileged_hall(content, *, hall="facts", layer, **store_kwargs) -> int:
    """A pre-01B-3 palace_store_hall(layer=0|1) result: a hall row at layer
    0/1 with admission NULL. Returns the hall id."""
    assert layer in (0, 1), "legacy privileged fixtures are for layer 0/1 only"
    hall_id = palace.palace_store_hall(content, hall, 2, **store_kwargs)
    _relayer("palace_halls", hall_id, layer)
    return hall_id
