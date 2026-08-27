"""AAAK identity-safety regressions (UI-TRUST-01 follow-up).

Root cause under test: aaak_compress() previously (a) carried explicit
name abbreviations ("Bino"->BIN, "Lumina"->LUM) and (b) substituted
abbreviations WITHOUT word boundaries, so short keys like "and"->"+"
shredded interior letters of unrelated words (standard -> st+ard,
handoff -> h+off, candidate -> c+idate, sandbox -> s+box). Together
these mangled identity strings -- including the co-author-trailer email
therealagentlumina@gmail.com -> therealagentLUM@gmail.com -- which then
propagated from stored memory onto real git commits (109a741, b3d0e9c).

Contract now: identities and emails pass through verbatim; whole-word
compression still works; word interiors are never touched.
"""

import pytest

from tools.palace import aaak_compress


def test_names_are_never_compressed():
    out = aaak_compress("Lumina paired with Bino on UI-TRUST-01")
    assert "Lumina" in out
    assert "Bino" in out
    assert "LUM" not in out.replace("LUMINA", "")  # no shorthand leakage
    assert "BIN" not in out


def test_commit_trailer_email_survives_verbatim():
    trailer = "Co-authored-by: Lumina <therealagentlumina@gmail.com>"
    assert aaak_compress(trailer) == trailer


def test_email_substring_case_insensitive_source_still_protected():
    # Even if a future mapping collides with an email-local substring,
    # lookarounds must protect letters embedded inside the address.
    addr = "someone@luminate.example"
    assert aaak_compress(addr) == addr


def test_word_interiors_are_never_shredded():
    raw = "standard standby handoff candidate landed sandbox withdraw"
    assert aaak_compress(raw) == raw


def test_whole_word_compression_still_works():
    out = aaak_compress("memory and database")
    assert out == "MEM + DB"


def test_case_insensitive_whole_words_still_compress():
    out = aaak_compress("Memory AND Database")
    assert out == "MEM + DB"


def test_phrase_entries_still_work():
    out = aaak_compress("task in progress today")
    assert out == "task WIP today"


def test_compression_power_preserved_overall():
    out = aaak_compress("The project is in progress and running")
    assert "PROJ" in out
    assert "WIP" in out
    assert "RUN" in out
    assert "+" in out
    assert "The" not in out  # filler stripping intact


@pytest.mark.parametrize(
    "corrupted",
    ["st+ard", "st+by", "h+off", "c+idate", "l+ed", "s+box"],
)
def test_historical_corruption_shapes_cannot_recur(corrupted):
    """Each historically observed mangled token must be unreachable from
    its natural-language source under the fixed compressor."""
    sources = {
        "st+ard": "standard",
        "st+by": "standby",
        "h+off": "handoff",
        "c+idate": "candidate",
        "l+ed": "landed",
        "s+box": "sandbox",
    }
    assert corrupted not in aaak_compress(sources[corrupted])


# ── Identifier-protection zone (Sol 5.6 corrective): whole-word boundaries
# cannot protect opaque identifiers because '@', '.', '/' are non-word chars.

def test_email_containing_mappable_words_is_sacred():
    out = aaak_compress("contact user@local.com about the project today")
    assert "user@local.com" in out          # email byte-for-byte
    assert "user@LOC.com" not in out
    assert "PROJ" in out                     # prose outside still compresses


def test_urls_are_sacred_even_when_segments_are_mappable():
    out = aaak_compress(
        "docs at https://local.example/project/python and memory notes"
    )
    assert "https://local.example/project/python" in out
    assert "https://LOC.example" not in out
    assert "MEM" in out                      # surrounding prose compressed


def test_filesystem_paths_are_sacred():
    out = aaak_compress(
        "/srv/local/project built w/ /home/bino/.local/share/lumina/data"
    )
    assert "/srv/local/project" in out
    assert "/home/bino/.local/share/lumina/data" in out
    assert "/srv/LOC/PROJ" not in out


def test_git_shas_and_hashes_are_sacred():
    sha = "bf237135deadbeef1234567890abcdef12345678"
    out = aaak_compress(f"landed commit {sha} after handoff")
    assert sha in out
    assert "handoff" in out                  # plain words around it still intact
    assert "h+off" not in out


def test_code_spans_are_sacred():
    out = aaak_compress("run `local --project python` then standby")
    assert "`local --project python`" in out
    assert "ST+BY" not in out and "st+by" not in out


def test_handles_are_sacred():
    out = aaak_compress("ping @lumina_ai and @bino_builds re: deploy")
    assert "@lumina_ai" in out
    assert "@bino_builds" in out


def test_identifiers_and_prose_compression_coexist():
    raw = (
        "The project is local and running; mirror https://a.example/local.git "
        "to /srv/mirror and email ops@local.host"
    )
    out = aaak_compress(raw)
    assert "https://a.example/local.git" in out
    assert "ops@local.host" in out
    assert "/srv/mirror" in out
    assert "PROJ" in out and "RUN" in out   # real prose compression intact
