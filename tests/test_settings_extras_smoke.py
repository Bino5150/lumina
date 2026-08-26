"""
Smoke test for four settings additions: stub Coming Soon tabs, the
Memory tab backup button, the iter_spin ceiling fix, and the cloud
model dropdown/refresh wiring.

Run headless from repo root:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_settings_extras_smoke.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import tempfile
import zipfile
from unittest.mock import patch

PASS, FAIL = [], []


def check(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail}")


def main():
    from PySide6.QtWidgets import QApplication, QComboBox
    app = QApplication.instance() or QApplication(sys.argv)

    import config
    from core.agent import LuminaAgent
    from core.personas import load_persona
    from ui.main_window import COLORS
    from ui.settings.general_tab import GeneralTab
    from ui.settings.memory_tab import MemoryTab, _build_memory_backup
    from ui.settings.coming_soon_tab import ComingSoonTab

    agent = LuminaAgent(owner=True, channel_id="settings-extras-smoke")
    persona = load_persona("personas/lumina.json")
    agent.apply_persona(persona)

    # ── ComingSoonTab ──────────────────────────────────────────────────
    img_tab = ComingSoonTab("Image Generation", "test subtitle", COLORS)
    oracle_tab = ComingSoonTab("Oracle", "test subtitle", COLORS)
    check("ComingSoonTab: Image Generation instantiates", img_tab is not None)
    check("ComingSoonTab: Oracle instantiates", oracle_tab is not None)

    # ── iter_spin ceiling ──────────────────────────────────────────────
    gt = GeneralTab(agent, COLORS)
    check("iter_spin: maximum is 100, not 20", gt.iter_spin.maximum() == 100,
          f"got {gt.iter_spin.maximum()}")
    gt.iter_spin.setValue(50)
    check("iter_spin: can actually be set above the old ceiling of 20",
          gt.iter_spin.value() == 50, f"got {gt.iter_spin.value()}")

    # ── model dropdown ────────────────────────────────────────────────
    check("cloud_model is now a QComboBox", isinstance(gt.cloud_model, QComboBox))
    check("cloud_model is editable (manual entry still works)",
          gt.cloud_model.isEditable())

    # A failed live refresh must not throw, must not mutate config, and must
    # not populate Anthropic's offline suggestions as provider results.
    gt.backend_combo.setCurrentText("anthropic")
    prev = getattr(config, "ANTHROPIC_API_KEY", "")
    prior_count = gt.cloud_model.count()

    class _FailedResponse:
        status_code = 401

        def raise_for_status(self):
            import requests
            raise requests.exceptions.HTTPError()

        def json(self):
            return {"error": {"message": "not authorized"}}

    try:
        with patch("requests.get", return_value=_FailedResponse()):
            gt._refresh_models()
        refresh_ok = True
    except Exception as e:
        refresh_ok = False
        print(f"    exception: {e}")
    check("_refresh_models(): does not throw with no key set", refresh_ok)
    check("_refresh_models(): does not leave a stray config mutation behind",
          getattr(config, "ANTHROPIC_API_KEY", "") == prev)

    check("_refresh_models(): failed live discovery preserves the prior list",
          gt.cloud_model.count() == prior_count, f"count={gt.cloud_model.count()}")
    check("_refresh_models(): offline suggestions are not provider results",
          gt.cloud_model.findText("claude-opus-4-7") == -1)
    check("_refresh_models(): failure is surfaced truthfully",
          "failed" in gt.status_lbl.text().lower(), gt.status_lbl.text())

    # ── memory backup ─────────────────────────────────────────────────
    tmp_data_dir = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp_data_dir, "memory"), exist_ok=True)
    with open(os.path.join(tmp_data_dir, "memory", "prefs.json"), "w") as f:
        f.write('{"test": true}')

    tmp_dest = tempfile.mktemp(suffix=".zip")
    try:
        _build_memory_backup(tmp_data_dir, tmp_dest)
        backup_ok = os.path.exists(tmp_dest)
    except Exception as e:
        backup_ok = False
        print(f"    exception: {e}")
    check("_build_memory_backup(): produces a zip file", backup_ok)

    if backup_ok:
        with zipfile.ZipFile(tmp_dest) as zf:
            names = zf.namelist()
        check("_build_memory_backup(): prefs.json is in the archive",
              any("prefs.json" in n for n in names), f"names={names}")
        check("_build_memory_backup(): credentials.json is NOT in the archive "
              "(lives outside DATA_DIR, must never appear here)",
              not any("credentials" in n for n in names))

    mt = MemoryTab(agent, COLORS)
    check("MemoryTab: instantiates with the new backup button wired",
          hasattr(mt, "_backup_memory"))

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
