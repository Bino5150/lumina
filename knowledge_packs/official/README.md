# Official Knowledge Packs

**Classification:** PUBLIC_PRODUCT_DOC / SHIPPED_KNOWLEDGE — this content ships
to every install and is retrievable by Lumina through her own tools. It is
information, never authority: see [docs/README.md](../../docs/README.md#how-to-read-this-documentation).

## `lumina_self_knowledge.json`

A JSON array of `{"title": "...", "content": "..."}` entries — concise,
self-contained notes about Lumina's own architecture (MemPalace, Halls,
Browser Companion, Reforge, tool tiers, and more), written to answer the
kind of question an owner or a curious user would actually ask.

Loaded by `core/knowledge_bootstrap.py` and seeded into the ordinary
Knowledge Base (`tools/knowledge.py`'s `knowledge` table) under the
reserved category `lumina-self-knowledge` on first startup for a given
data directory. No new indexing format, no new trust channel — retrieval
goes through the exact same `list_knowledge`/`search_knowledge`/
`read_knowledge` tools as anything the owner or Lumina saves herself.

## Adding or editing an entry

- Keep each entry short and self-contained (a few sentences) — these are
  retrieval chunks, not manual pages. The full manual lives in `docs/`.
- State facts, not instructions. Never phrase an entry as granting
  permission or asserting that something is authorized — see
  `core/knowledge_bootstrap.py`'s module docstring for why that boundary
  is structural, not just a style preference.
- The bootstrap only ever seeds a data directory once (see that same
  docstring's "Idempotency model"): adding an entry here does not deliver
  it to an install that has already been seeded. That is a known,
  documented limitation of this first pass, not an oversight.
