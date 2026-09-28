"""DOCS-01B / DOCS-PERSONA-ROSTER-PROVENANCE-01 -- README.md's "Included
Personas" list and docs/personas-and-skills.md must name exactly the
selectable personas that SHIP.

DOCS-01A found README.md listing 11 personas while only 3 selectable files
actually shipped -- a stale roster left over from before third-party
character personas were pulled from the public release. DOCS-01B made that
class of drift fail the build by reading the selectable roster straight from
core.personas.list_personas() (the function the desktop Persona picker
uses).

DOCS-PERSONA-ROSTER-PROVENANCE-01: "on disk" is not "shipped". The dev line
legitimately carries local-only personas the public release never ships, so
the roster is now classified by PATH MEMBERSHIP against two sources:

- metadata/shipped_baseline_hashes.json -- the release-derived, git-
  generated positive set of persona paths that ship. Only its KEYS are
  used here: persona bytes intentionally differ between release and dev
  (e.g. lumina.json, discord_template.json), so hashes answer a different
  question and are never compared for roster/docs purposes.
- metadata/local_personas.json -- OPTIONAL, dev-only overlay declaring
  persona paths intentionally local to that line. No hashes, no provenance
  or release identity, no authority beyond roster classification. Absent
  on release, where the local set is therefore empty.

Invariant, for every selectable persona file physically present:
    path in shipped XOR path in declared-local
Neither (e.g. a new release persona nobody added to the shipped manifest)
and both (a declaration trying to hide a shipped persona) fail closed, as do
missing, duplicate, malformed, or out-of-directory declarations. Every
shipped selectable persona must then be documented.
"""
import json
import os
import re

import pytest

import core.personas as personas_mod
from core.personas import list_personas

README_PATH = "README.md"
DOCS_PATH = "docs/personas-and-skills.md"
MANIFEST_REL = os.path.join("metadata", "shipped_baseline_hashes.json")
LOCAL_DECL_REL = os.path.join("metadata", "local_personas.json")
LOCAL_DECL_KEY = "local_personas"

# One flat persona file: personas/<name>.json -- no subdirectories, no
# traversal, no absolute paths, nothing outside the persona directory.
_PERSONA_PATH_RE = re.compile(r"personas/[A-Za-z0-9_.\-]+\.json")


class RosterBoundaryError(AssertionError):
    pass


def _repo_root():
    return os.path.dirname(personas_mod.PERSONAS_DIR)


def _shipped_persona_paths(manifest_path):
    with open(manifest_path, encoding="utf-8") as fh:
        files = json.load(fh)["files"]
    # Membership only: the manifest's hash values are deliberately ignored.
    return {path for path in files if path.startswith("personas/")}


def _declared_local_paths(decl_path, personas_dir):
    """Empty set when the overlay is absent (release). Otherwise a strictly
    validated set of flat persona paths that exist on disk."""
    if not os.path.exists(decl_path):
        return set()
    try:
        with open(decl_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise RosterBoundaryError(f"{decl_path} is not valid JSON: {type(e).__name__}")
    if not isinstance(data, dict) or set(data) != {LOCAL_DECL_KEY}:
        raise RosterBoundaryError(
            f"{decl_path} must be an object with exactly one key, {LOCAL_DECL_KEY!r}"
        )
    entries = data[LOCAL_DECL_KEY]
    if not isinstance(entries, list):
        raise RosterBoundaryError(f"{decl_path}: {LOCAL_DECL_KEY!r} must be a list")
    declared = set()
    for entry in entries:
        if not isinstance(entry, str) or not _PERSONA_PATH_RE.fullmatch(entry):
            raise RosterBoundaryError(
                f"{decl_path}: {entry!r} is not a flat personas/<name>.json path"
            )
        if entry in declared:
            raise RosterBoundaryError(f"{decl_path}: duplicate declaration {entry!r}")
        if not os.path.isfile(os.path.join(personas_dir, os.path.basename(entry))):
            raise RosterBoundaryError(f"{decl_path}: declared {entry!r} does not exist")
        declared.add(entry)
    return declared


def classify_roster(personas_dir=None, manifest_path=None, decl_path=None):
    """Return (shipped, local): {persona_path: display_name} for every
    selectable (non-channel_bound) persona list_personas() returns, split by
    path membership. Raises RosterBoundaryError on any boundary violation."""
    personas_dir = personas_dir or personas_mod.PERSONAS_DIR
    root = os.path.dirname(personas_dir)
    manifest_path = manifest_path or os.path.join(root, MANIFEST_REL)
    decl_path = decl_path or os.path.join(root, LOCAL_DECL_REL)

    shipped_paths = _shipped_persona_paths(manifest_path)
    local_paths = _declared_local_paths(decl_path, personas_dir)

    overlap = sorted(shipped_paths & local_paths)
    if overlap:
        raise RosterBoundaryError(
            f"declared-local personas overlap the shipped manifest: {overlap}"
        )

    shipped, local, unclassified = {}, {}, []
    for persona in list_personas(include_channel_bound=False):
        path = "personas/" + os.path.basename(persona["_file"])
        if path in shipped_paths:
            shipped[path] = persona["name"]
        elif path in local_paths:
            local[path] = persona["name"]
        else:
            unclassified.append(path)
    if unclassified:
        raise RosterBoundaryError(
            f"selectable personas neither shipped (in {MANIFEST_REL}) nor declared "
            f"local (in {LOCAL_DECL_REL}): {sorted(unclassified)}"
        )
    return shipped, local


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── The real checkout ─────────────────────────────────────────────────────────

def test_every_selectable_persona_is_shipped_xor_declared_local():
    shipped, local = classify_roster()
    all_names = {p["name"] for p in list_personas(include_channel_bound=False)}
    assert set(shipped.values()) | set(local.values()) == all_names
    assert shipped, "no shipped selectable persona found -- manifest wiring is broken"


def test_readme_included_personas_matches_shipped_roster():
    shipped_names = set(classify_roster()[0].values())
    text = _read(README_PATH)
    section_match = re.search(
        r"## Included Personas\n(.*?)\n##", text, re.DOTALL
    )
    assert section_match, "README.md's '## Included Personas' section is missing"
    section = section_match.group(1)
    for name in shipped_names:
        assert name in section, (
            f"README.md's Included Personas section doesn't mention shipped persona {name!r}"
        )
    # Guard the other direction too: a name-shaped mention of a persona
    # that doesn't ship is exactly DOCS-01A's original bug.
    known_absent = {
        "Ultron", "Rick Sanchez", "HAL 9000", "KITT", "Optimus Prime",
        "Skynet", "Neil deGrasse Tyson", "Bender",
    }
    for name in known_absent:
        assert name not in section, (
            f"README.md's Included Personas section names {name!r}, which does not "
            f"ship -- this is the stale-roster bug DOCS-01A found"
        )


def test_manual_persona_page_matches_shipped_roster():
    shipped_names = set(classify_roster()[0].values())
    text = _read(DOCS_PATH)
    for name in shipped_names:
        assert name in text, (
            f"{DOCS_PATH} doesn't mention shipped persona {name!r}"
        )


# ── Boundary rules, on synthetic release/dev shapes ───────────────────────────

@pytest.fixture
def shape(tmp_path, monkeypatch):
    """A throwaway repo shape: personas/, metadata/ manifest, optional overlay."""
    pdir = tmp_path / "personas"
    pdir.mkdir()
    (tmp_path / "metadata").mkdir()
    monkeypatch.setattr(personas_mod, "PERSONAS_DIR", str(pdir))

    def build(on_disk, shipped, local=None, raw_local=None, channel_bound=()):
        for stem in on_disk:
            data = {"name": stem.title()}
            if stem in channel_bound:
                data["channel_bound"] = True
            (pdir / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
        manifest = {"files": {f"personas/{s}.json": "0" * 64 for s in shipped}}
        manifest["files"]["skills/x.md"] = "0" * 64
        (tmp_path / "metadata" / "shipped_baseline_hashes.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        decl = tmp_path / "metadata" / "local_personas.json"
        if raw_local is not None:
            decl.write_text(raw_local, encoding="utf-8")
        elif local is not None:
            decl.write_text(json.dumps({LOCAL_DECL_KEY: local}), encoding="utf-8")
        return classify_roster
    return build


def test_release_shape_without_overlay_classifies_by_manifest(shape):
    classify = shape(on_disk=["lumina", "mara", "discord"], shipped=["lumina", "mara", "discord"],
                     channel_bound=("discord",))
    shipped, local = classify()
    assert set(shipped.values()) == {"Lumina", "Mara"} and local == {}


def test_release_persona_missing_from_manifest_still_fails(shape):
    """The invariant Option B exists to keep: a new release persona nobody
    added to the shipped manifest can't silently escape the docs guard."""
    classify = shape(on_disk=["lumina", "newcomer"], shipped=["lumina"])
    with pytest.raises(RosterBoundaryError, match="neither shipped"):
        classify()


def test_dev_shape_excludes_exactly_the_declared_local_personas(shape):
    classify = shape(on_disk=["lumina", "bender", "kitt"], shipped=["lumina"],
                     local=["personas/bender.json", "personas/kitt.json"])
    shipped, local = classify()
    assert set(shipped) == {"personas/lumina.json"}
    assert set(local) == {"personas/bender.json", "personas/kitt.json"}


def test_declaration_overlapping_a_shipped_persona_fails(shape):
    classify = shape(on_disk=["lumina", "bender"], shipped=["lumina"],
                     local=["personas/bender.json", "personas/lumina.json"])
    with pytest.raises(RosterBoundaryError, match="overlap the shipped manifest"):
        classify()


def test_declared_local_persona_that_does_not_exist_fails(shape):
    classify = shape(on_disk=["lumina"], shipped=["lumina"], local=["personas/ghost.json"])
    with pytest.raises(RosterBoundaryError, match="does not exist"):
        classify()


def test_duplicate_declaration_fails(shape):
    classify = shape(on_disk=["lumina", "bender"], shipped=["lumina"],
                     local=["personas/bender.json", "personas/bender.json"])
    with pytest.raises(RosterBoundaryError, match="duplicate"):
        classify()


@pytest.mark.parametrize("raw, reason", [
    ("not json at all", "not valid JSON"),
    (json.dumps(["personas/bender.json"]), "exactly one key"),
    (json.dumps({"local_personas": "personas/bender.json"}), "must be a list"),
    (json.dumps({"local_personas": ["personas/bender.json"], "git_commit": "abc"}),
     "exactly one key"),
    (json.dumps({"local_personas": [7]}), "not a flat"),
])
def test_malformed_declaration_fails_for_its_own_reason(shape, raw, reason):
    # Each case must fail on the malformation itself, not merely because the
    # unparsed persona then lands in the "neither" bucket downstream.
    classify = shape(on_disk=["lumina", "bender"], shipped=["lumina"], raw_local=raw)
    with pytest.raises(RosterBoundaryError, match=reason):
        classify()


@pytest.mark.parametrize("entry", [
    "../personas/bender.json", "personas/../bender.json", "personas/sub/bender.json",
    "/abs/personas/bender.json", "bender.json", "personas/bender.txt", "personas/",
])
def test_declaration_outside_the_persona_directory_fails(shape, entry):
    classify = shape(on_disk=["lumina", "bender"], shipped=["lumina"], local=[entry])
    with pytest.raises(RosterBoundaryError, match="not a flat"):
        classify()


def test_persona_bytes_are_never_compared(shape, tmp_path):
    """Shared personas intentionally differ between release and dev; a
    manifest hash that doesn't match the file must not change membership."""
    classify = shape(on_disk=["lumina"], shipped=["lumina"])
    assert (tmp_path / "personas" / "lumina.json").read_bytes()  # hash above is all zeros
    shipped, _ = classify()
    assert set(shipped) == {"personas/lumina.json"}


def test_channel_bound_personas_need_no_classification(shape):
    classify = shape(on_disk=["lumina", "discord"], shipped=["lumina"],
                     channel_bound=("discord",))
    shipped, local = classify()
    assert set(shipped) == {"personas/lumina.json"} and local == {}
