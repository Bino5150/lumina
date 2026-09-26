# Memory

Lumina's memory is several related subsystems that work together, not one
database. This page covers each one and what it actually guarantees.

## Basic Agent Memory

A flat, weighted "let me jot this down" memory (`tools/memory.py`):
`save_memory`, `search_memory`, `get_recent_memories`, `delete_memory`.
Simple, direct, no layering.

## The MemPalace

A three-layer hierarchy, deliberately more structured than "embed
everything, retrieve top-K":

- **Wings → Rooms → Closets → Drawers** — a hierarchical container
  structure Lumina organizes memories into.
- **Layer 0 & 1 — Permanent knowledge.** Core identity facts and
  structural reality that never expires.
- **Layer 2 — Decaying episodic memory.** Recent, session-based
  knowledge, weighted by a temporal decay function (default λ=0.0083 —
  roughly 78% retention after 30 days, 61% after 60, 47% after 90). Old
  entries fade gracefully rather than dropping off a cliff. The decay
  constant is tunable.
- **Layer 3 — Unlimited, verbatim, on-demand.** Not part of the automatic
  context injection; retrieved only when explicitly recalled.

Content is compressed (AAAK compression) to fit more into fewer tokens,
while exact names, emails, URLs, hashes, and other opaque identifiers are
protected from lossy abbreviation.

**Search is keyword-based, not full-text-indexed.** `palace_recall` uses a
plain SQL `LIKE '%query%'` match — there is no FTS5 table backing the
MemPalace. (FTS5 genuinely does back two *other* subsystems — see [Chat
History Search](#chat-history-search) and [Skills](personas-and-skills.md#skills)
below — but not this one.)

**Halls** are a separate, cross-cutting stream from the Wings hierarchy:
short factual entries tagged as `facts`, `events`, `preferences`,
`advice`, or `discoveries`, retrieved via the `palace_hall` tool. Think of
Wings/Rooms/Closets/Drawers as *where* something lives and Halls as a
running index of a few specific kinds of fact regardless of where they
live.

**Write invariant.** Nothing Dreaming or Compaction writes autonomously is
promoted straight to permanent (Layer 0/1) identity — both of today's
autonomous writers (Dreaming's idle-sweep, and manual `/compact`) always
write to Layer 2. This is enforced by both call sites doing the right
thing today, not by the storage function itself refusing a different
layer — worth knowing if you're evaluating how hard a guarantee this is.

Six tools cover the MemPalace: `palace_remember`, `palace_hall`,
`palace_recall`, `palace_status`, `palace_review_writes`,
`palace_undo_write`.

## Context Compaction

Every context window has a ceiling. Most agents handle it by dropping the
oldest turns and losing them. Lumina's raw messages are already persisted
and full-text searchable (see Chat History below), so nothing that rolls
off is gone forever — but she won't think to search for something she
doesn't know happened. Context Compaction closes that gap: as the trim
loop drops messages to stay under budget, the outgoing batch is
summarized in the background and written into MemPalace Layer 2, tagged
and pinned to the session it came from. Off by default
(`CONTEXT_COMPACTION_ENABLED` in Settings → General) until you turn it on.
The full, uncompressed chat history is never deleted by this — compaction
only affects what's actively injected into context.

## `/context rebuild`

A separate, owner-facing operator command (see [Operator
Commands](operator-commands.md)) that does something more deliberate than
passive compaction: an LLM-compiled "continuity checkpoint" reconstruction
of live context from the durable transcript plus machine-recorded facts,
with staleness and emergency-stop guards and a before/after receipt
grounded in what actually happened rather than model narration. Backed by
`core/context_rebuild.py`, `core/continuity_compiler.py`,
`core/context_transaction.py`, and `core/context_checkpoints.py`. This
command family is desktop-only by design — it isn't reachable from
Telegram, Discord, or any tool-call path.

This mechanism has a name — **Reforge** — and a full technical and
empirical reference of its own, including live-tested context-reduction
results and an honestly-reported known limitation: see
[Reforge](reforge.md).

## Dreaming

When a session goes idle, Lumina reviews what was actually said and
worked on, distills it into a summary, and writes it to a dedicated
"nightstand" space — deliberately separate from her curated MemPalace
wings, and always at Layer 2 (see the write invariant above). Dreaming is
**on by default** (`DREAM_SWEEP_ENABLED`). A dream is a first draft, not a
fact: fully reviewable and undoable, never silently promoted to something
she "just knows."

## My Human

Three fields, not two, feed Lumina's picture of who you are:

1. **Your own bio** (`human_bio`) — what you write about yourself in
   Settings → User Profile. Authoritative; Lumina never contradicts it.
2. **Curated notes** (`human_profile_curated`) — also edited in Settings →
   User Profile, but populated by Lumina herself. Riding the same
   idle-sweep mechanism as Dreaming, she periodically resynthesizes an
   evolving picture of what you're working on from conversations, without
   you filling out a form. Reconciliation with your own bio happens once,
   at curation time — your stated bio always wins, never overridden
   mid-conversation.
3. **Public bio** (`human_bio_public`) — set separately in Settings →
   Communications, not User Profile. This is what a non-owner/external
   channel (Telegram messages *to* others, Discord) sees in place of the
   two private fields above. It exists specifically so a stranger-facing
   channel doesn't get your private bio or Lumina's private notes about
   you by default.

## Chat History Search

Full-text search over the raw message log — genuinely FTS5-indexed
(unlike the MemPalace), available as an on-demand tool
(`search_chat_history`, plus `get_chat_session`/`list_recent_chats`).
Falls back to a plain `LIKE` query only if FTS5 returns nothing or errors.
Never auto-injected — Lumina has to decide to search it.

## Knowledge Base

For explicit reference material rather than conversational memory — both
you and Lumina can save documents, notes, and datasets here
(`save_knowledge`, `list_knowledge`, `read_knowledge`, `search_knowledge`,
`delete_knowledge`, plus a small people-directory: `save_person`,
`search_people`). Like the MemPalace, search here is `LIKE`-based, not
FTS5. Permanently stored — unlike "chat with your document" in the chat
window itself, it's there when you come back to it later.

A small set of entries under the reserved category `lumina-self-knowledge`
ships with the app and seeds itself into a fresh install automatically —
concise, factual notes about Lumina's own architecture (MemPalace, Halls,
Browser Companion, Reforge, and more), retrievable through the exact same
`list_knowledge`/`search_knowledge`/`read_knowledge` tools as anything
else stored here. There's nothing special about it structurally: it's
ordinary information, tagged with the same tool-output provenance as any
other tool result, never elevated to owner authority just because it
shipped with the app. Deleting an entry is respected permanently — it
won't be silently reinstalled on the next launch unless the entire
category is emptied out.

## Backup

**Backup Memory** in Settings is a one-click button
(`core/memory_backup.py`) that checkpoints the database
(`PRAGMA wal_checkpoint(TRUNCATE)`) and zips the entire data directory —
chat history, MemPalace, flat memories, Knowledge Base, the pending-action
audit log, custom tools, projects, and preferences. Credentials never make
it in: `credentials.json` lives outside the data directory by design.

There is no separate "restore" UI for this yet — the backup is a file you
keep.
