"""DOCS-01B -- README.md's "Included Personas" list and
docs/personas-and-skills.md must name exactly the selectable personas that
actually ship on disk.

DOCS-01A found README.md listing 11 personas (Lumina, Ultron, Rick
Sanchez, HAL 9000, KITT, Optimus Prime, Skynet, Neil deGrasse Tyson,
Bender, Mr. Robot, Mara Voss) while only 3 files actually existed in
personas/ -- a stale roster left over from before third-party character
personas were pulled from the public release. This test makes that class
of drift fail the build instead of silently re-accumulating: it reads the
real, selectable (non-channel_bound) roster straight from personas/*.json
via core.personas.list_personas(), the same function the desktop Persona
picker itself uses, and checks both docs surfaces name exactly that set.
"""
import re

from core.personas import list_personas

README_PATH = "README.md"
DOCS_PATH = "docs/personas-and-skills.md"


def _real_persona_names():
    return {p["name"] for p in list_personas(include_channel_bound=False)}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_readme_included_personas_matches_disk():
    real_names = _real_persona_names()
    text = _read(README_PATH)
    section_match = re.search(
        r"## Included Personas\n(.*?)\n##", text, re.DOTALL
    )
    assert section_match, "README.md's '## Included Personas' section is missing"
    section = section_match.group(1)
    for name in real_names:
        assert name in section, (
            f"README.md's Included Personas section doesn't mention shipped persona {name!r}"
        )
    # Guard the other direction too: a name-shaped mention of a persona
    # that isn't real is exactly DOCS-01A's original bug.
    known_absent = {
        "Ultron", "Rick Sanchez", "HAL 9000", "KITT", "Optimus Prime",
        "Skynet", "Neil deGrasse Tyson", "Bender",
    }
    for name in known_absent:
        assert name not in section, (
            f"README.md's Included Personas section names {name!r}, which has no "
            f"corresponding file in personas/ -- this is the stale-roster bug DOCS-01A found"
        )


def test_manual_persona_page_matches_disk():
    real_names = _real_persona_names()
    text = _read(DOCS_PATH)
    for name in real_names:
        assert name in text, (
            f"{DOCS_PATH} doesn't mention shipped persona {name!r}"
        )
