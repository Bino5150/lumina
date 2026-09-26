"""CONFIG-EXAMPLE-PARITY-01 -- config.example.py must be a truthful
fresh-install template.

DOCS-01A's inventory found config.example.py had drifted behind config.py:
several feature commits (TEST-DATA-ISOLATION-01, BACKEND-CONTRACT-01A,
MB-11 context compaction, the human-profile-curation toggle, Chatterbox's
device setting, the per-backend TOOL_RESULT_MAX_CHARS refactor) touched
config.py without updating config.example.py alongside it. Two of the gaps
were live crashes: ui/settings/general_tab.py reads
config.CONTEXT_COMPACTION_ENABLED directly (no getattr fallback), and
ui/settings/user_profile_tab.py reads config.HUMAN_PROFILE_CURATION_ENABLED
the same way -- so a config.py produced by `cp config.example.py config.py`
on a brand-new install raised AttributeError the first time General or
User Profile Settings was opened.

This file encodes the required-public-config contract going forward: every
SCREAMING_CASE constant the tracked config.py defines must also have a
representation in config.example.py. It compares attribute *names*, not
values, so legitimate per-install value differences (a hand-edited
LM_STUDIO_BASE_URL, a real cloud API key stashed outside the tracked file,
etc.) never fail this test -- only a genuinely missing attribute does.
"""
import importlib.util
import os
import re

import pytest

import config as real_config

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PUBLIC_CONST_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _public_config_names(module):
    return {name for name in dir(module) if _PUBLIC_CONST_RE.match(name)}


def _load_example_as_fresh_config(tmp_path):
    """Exec config.example.py exactly the way a fresh install does: `cp
    config.example.py config.py`, then import it. Relies on the
    LUMINA_DATA_DIR/LUMINA_TESTING/LUMINA_SECRETS_PATH isolation
    tests/conftest.py already establishes process-wide -- this only needs
    its own copy of the file so the fresh module's own BASE_DIR/__file__
    don't collide with the real config.py's."""
    example_src = os.path.join(REPO_ROOT, "config.example.py")
    dest = tmp_path / "config.py"
    with open(example_src, encoding="utf-8") as f:
        dest.write_text(f.read())
    spec = importlib.util.spec_from_file_location(
        f"_fresh_install_config_{tmp_path.name}", dest
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fresh_config(tmp_path):
    return _load_example_as_fresh_config(tmp_path)


def test_example_config_has_every_public_attribute_from_real_config(fresh_config):
    """A gap here means a config.py built fresh from config.example.py is
    missing something the tracked config.py defines -- the general form of
    the bug CONFIG-EXAMPLE-PARITY-01 fixed."""
    real_names = _public_config_names(real_config)
    example_names = _public_config_names(fresh_config)
    missing = sorted(real_names - example_names)
    assert not missing, (
        "config.example.py is missing public config attributes present in "
        f"config.py: {missing}. A fresh install built from `cp "
        "config.example.py config.py` would not define these."
    )


@pytest.mark.parametrize("attr", [
    "CONTEXT_COMPACTION_ENABLED",
    "CONTEXT_COMPACTION_BATCH_TOKENS",
    "HUMAN_PROFILE_CURATION_ENABLED",
])
def test_fresh_install_config_survives_direct_settings_reads(fresh_config, attr):
    """ui/settings/general_tab.py and ui/settings/user_profile_tab.py read
    these attributes directly off config (no getattr fallback) the moment
    their Settings tab opens -- the two confirmed crash paths from
    DOCS-01A's inventory."""
    assert hasattr(fresh_config, attr)
