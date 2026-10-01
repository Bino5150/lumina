# Memory

Lumina's memory is several related subsystems that work together, not one
database. This page covers each one and what it actually guarantees.

## Basic Agent Memory

A flat, weighted "let me jot this down" memory (`tools/memory.py`):
`save_memory`, `search_memory`, `get_recent_memories`, `delete_memory`.
Simple, direct, no layering.

Each saved memory is also written into the MemPalace (below), and the two
are written together or not at all. Deleting a memory — from Settings →
Memory, or by approving a staged `delete_memory` — removes the Palace
copies saved with it in the same step: its drawer, its segment of the
shared closet, and any Hall entry. What deletion does and doesn't cover:

- If the shared closet can be rebuilt exactly from the memories that
  remain, it is. If it can't (its stored text has drifted from what its
  memories would render today), it isn't rewritten; it's **withheld** —
  kept out of Lumina's injected memory and out of Palace search — until
  it's reviewed or its last memory is deleted.
- Memories saved before this linkage existed are deleted from the flat
  list only. Their Palace copy, if any, isn't guessed at or removed, and
  the delete result says so.
- A Palace copy you've promoted to Layer 0/1 (below) is removed with its
  memory like any other linked copy.
- It's exact for the Palace copies linked to that memory. The same text
  can still exist in chat history, dream summaries, backups, or telemetry.

## The MemPalace

A three-layer hierarchy, deliberately more structured than "embed
everything, retrieve top-K":

- **Wings → Rooms → Closets → Drawers** — a hierarchical container
  structure Lumina organizes memories into.
- **Layer 0 & 1 — Permanent knowledge.** Core identity facts and
  structural reality that never expires. These layers are *privileged*:
  they're injected into every turn and exempt from decay, so Lumina can't
  place anything there herself — see [Promoting a record to Layer
  0/1](#promoting-a-record-to-layer-01) below.
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

**Write invariant.** Nothing Lumina writes on her own reaches Layer 0/1.
Both autonomous writers (Dreaming's idle-sweep, and manual `/compact`)
write to Layer 2, and so does everything `save_memory` mirrors into the
Palace. If Lumina asks `palace_remember` or `palace_hall` for layer 0 or
1, the memory is stored at Layer 2, the tool result says so, and the
request is staged for you (below); only an exact integer 0 or 1 counts as
such a request — `true`, `1.0` and `"1"` don't. This is enforced by the
storage layer itself, not by each caller behaving: the Palace's generic
write path refuses any layer below 2 (and any layer that isn't a real
integer), and the only writers that can occupy Layer 0/1 are the one-time
startup identity seed and an owner-approved promotion. Layer 0/1 records
from older versions stay exactly where they are — they aren't demoted,
re-stamped, or guessed at.

### Promoting a record to Layer 0/1

Asking for a privileged layer isn't the same as being granted one. When
Lumina asks for layer 0 or 1, the memory lands at Layer 2 and a
**promotion request** appears in Settings → Tools → **Pending Actions**.
The request is only a pointer to one drawer or one Hall entry. It is never
an approval, and nothing in it — its fields, a "confirmed" flag, or the
audit log beside it — is trusted, because those are ordinary files Lumina
can write to.

Selecting the request shows the record's **live** state, read from the
Palace at that moment rather than taken from the request: where it lives,
its current layer, whether it's lower-trust, what will happen to the closet
it sits in, and its exact text. **Approve** opens a review of that state,
and your click on the dialog's Approve button is the only thing that can
promote the record. Pressing Enter or closing the dialog does nothing.

- **One record, exactly what you saw.** The approval is bound to that one
  drawer or Hall entry — its content, layer, trust and provenance, its
  location, and the layer you're approving. If anything changed after you
  opened the review (even its trust, or a neighbouring drawer in the same
  closet), the promotion is refused and nothing is written. An approval
  works once and expires after two minutes.
- **Layer only.** Promotion moves the record and changes nothing else. A
  lower-trust record that becomes Layer 1 is still lower-trust and is still
  shown to Lumina framed as data, not as your own words.
- **Closets.** A promoted drawer gets its own closet at the new layer. If
  it shared a Layer 2 closet, the drawers left behind are rebuilt byte for
  byte when that closet matches its drawers; if it has drifted, it's left
  untouched and **withheld** from Lumina's memory until reviewed — the same
  rule deletion uses. A record in an already-withheld closet can't be
  promoted.
- **Receipt.** Every promotion writes a receipt (what was promoted, the
  layers, the state you approved) and marks the new record as an owner
  promotion. The startup identity seed carries its own mark; records from
  older versions carry neither, and none is inferred for them.

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

**Manual `/compact`** is the on-demand version, for a chat that has grown
long. It works whether or not that setting is on (it's owner-only and
idle-only). It summarizes everything except your newest two turns into a
Layer 2 nightstand summary and marks a checkpoint, so when you reopen that
chat (or restart Lumina) only the summary plus the messages after the
checkpoint go back into context. The whole transcript is still shown on
screen and is never rewritten or deleted. Only a checkpoint written by
`/compact` itself, which carries an internal provenance stamp, can shorten
what gets reloaded: a memory Lumina saves herself that merely looks like a
checkpoint has no such power, and a checkpoint that has been undone or
quarantined stops having it. Checkpoints written by versions before that
stamp existed can't be verified, so they're ignored: reopening such a chat
reloads its fuller history into context, nothing is lost, and running
`/compact` again writes a fresh, trusted checkpoint.

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
fact: reviewable (`palace_review_writes`) and never silently promoted to
something she "just knows." Dreams and compaction summaries written by this
version carry a provenance stamp and can be undone with
`palace_undo_write`; older ones predate that stamp, so undo refuses them
rather than guess (and, for a `/compact` checkpoint, it's the same stamp
that lets it shorten a reloaded chat — see Context Compaction above).

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
shipped with the app. Deleting an entry — one of them, or all of
them — is respected permanently; it won't be silently reinstalled on a
later launch. Installation is tracked by a separate marker file, not by
whether the rows still exist, specifically so deleting everything can
never look like "never installed" and trigger a silent reseed.

## Backup

**Backup Memory** in Settings is a one-click button
(`core/memory_backup.py`) that checkpoints the database
(`PRAGMA wal_checkpoint(TRUNCATE)`) and zips the entire data directory —
chat history, MemPalace, flat memories, Knowledge Base, the pending-action
audit log, custom tools, projects, and preferences. The normal
`credentials.json` and ChatGPT sign-in stores live outside the data
directory by design. Symbolic links inside the data directory are
not followed, and a ChatGPT session file copied or moved into the data
directory is left out of the archive when its specific session or host schema
field is recognized, including wrapped and escaped copies. Memory Backup
reports the count and names of possible session or ambiguous files it excluded. It does
not exclude ordinary JSONL audit logs or benign large JSON merely because
they mention ChatGPT, schema, Unicode escapes or exceed 1 MiB. Deep JSON that
cannot be parsed safely is excluded as ambiguous. The bytes checked are the bytes archived; a
source-path replacement during backup cannot substitute later bytes.
Keep other copies of credentials out of the data directory.

There is no separate "restore" UI for this yet — the backup is a file you
keep.
