# Historical recovery acceptance contract

This is the acceptance contract for Restore. Verified Export implements no
Restore. The first slice, an isolated staging restore into a new, empty,
non-production data folder, and its Settings workflow are described at the end of
this document; production activation and break-glass recovery remain
unimplemented.
`historical_recovery_contract()` is a pure contract assessment of a manifest; it
does not verify an archive or grant recovery/activation permission. A future
consumer must first verify immutable archive bytes and validate each selected
recovery unit.

## Required disaster scenario

A replacement machine starts from a fresh GitHub installation, with no Palace,
Skills history, conversations, preferences, installation markers, Project history
or user overlays. The owner supplies the only surviving archive. Supported
historical content must be recoverable without destination records, a lost signing
key, an official-package catalog match or independently provable historical facts.
Relationships and path-dependent state must survive the recovery plan and local
rebind. Missing marker history must remain unknown, never proof of first run.

Recovered history supplies information. It does not automatically authenticate
official packages, promote a trusted owner, authorize credentials, approve custom
tools or pending actions, or clear a suspension. Unknown bounded optional history
is retained opaquely, including installation/deletion markers. Unknown required
features block affected activation until compatible handling exists; they must
not cause silent data loss or a blanket ban on supported historical recovery.

## Owner-local emergency recovery ceremony

1. Designate the source archive locally and preview validated inventory, unknown
   versions, omissions, damaged units, provenance limits and proposed destination
   changes. Integrity verification is separate from semantic coverage and C1
   enforcement compatibility. Unsupported physical formats require a deliberate
   compatible reader; no version rewriting or weakened verifier.
2. Require explicit consent to the concrete bounded plan. Warnings must explain
   uncertainty without demanding corroborating history that died with the machine.
   No mandatory ZIP password or historical signature is required for local
   recovery. Optional authenticated encryption remains a separate custody feature.
3. Exclude every live writer, including desktop, CLI/headless, bridges, evaluation,
   approvals and any shared-soul process. Owner consent cannot waive writer
   exclusion. Take an emergency destination snapshot where safely possible; if
   unavailable, disclose that limit and preserve source evidence before consent.
4. Stage validated compatible units, preserve previous-good destination state and
   apply transactional publication/rollback. A partially damaged archive must have
   a supported path to recover independently validated units without forcing damaged
   data into place. Never bypass fundamental ZIP/resource/containment checks. Do not
   claim a damaged whole archive is verified; record each recovered and skipped
   unit, dependency group and reason. SQLite databases and linked relational state
   require coherent units, not arbitrary row/file salvage.
5. Reconcile restrictions restrictively, rebind paths/tools/credentials locally,
   and start safely. Historical approvals stay inactive and communications stay
   off until explicit owner-local setup. Startup must not replay imported work.

## Mandatory C1 fence

The C1 owner controls the actual APIs and recovery semantics. Incomplete State
Export uses the accepted metadata-only `core.suspension_backup` exclusion API.
It exports ordinary history even with an initialized, actively suspended store,
without opening or capturing protected state. The pre-capture descriptor supplies
protected paths/inodes and the exact manifest fence record; a final boundary
recheck gates publication. This is exclusion, not a C1 snapshot or continuity
proof. Complete Verified Agent Backup remains unavailable. No C1 source is
modified and no Restore is implemented by this integration.

For future Restore, healthy destination C1 remains authoritative and unchanged.
Archived C1 is evidence only, never copied to live store/identity filenames. If
destination C1 is absent, recovery must create a durable nonempty restore trace
even when the archive omits C1. That prevents first-run Initialize; owner-local
Recover must establish the C1 owner's restrictive owner/unknown-scope suspension
and fresh identity/epoch rules. Unsupported minimum enforcement blocks activation.
Recovery never resumes work, clears restrictions, re-enables Telegram or edits
C1 selectors/epochs to manufacture compatibility.

## Required future acceptance receipts

An isolated full recovery exercise must verify fresh-install history, Palace and
Skills relationships, conversations, Project rebind and safe tools/startup. A
damaged-archive exercise must verify supported validated-unit salvage and unchanged
previous-good destination units. Include live-writer rejection, rollback failure
paths, unknown optional/required features, installation tombstones, healthy versus
absent/unavailable C1, missing restore traces, and incompatible enforcement. Report
native exits, exact source/destination identities, recovered/skipped units and
remaining acceptance gaps. Export integrity PASS alone cannot satisfy these tests.

## Isolated staging restore (engineering slice)

`core/agent_backup_restore.py` implements the first slice of this contract for
an isolated destination only. Its only user interface is the Settings workflow
described under "Settings cockpit" below, and it refuses, regardless
of test mode, any destination or install folder inside
`~/.local/share/lumina`, `~/.local/share/lumina-release`, `~/.config/lumina`, the
platform data folder, the running process's `LUMINA_DATA_DIR`, or the running
installation.

Lifecycle: `plan_restore()` runs the shipped intake and preview, applies the
owner's per-unit selection (`recover`, `recover_keep_destination`, `skip`) and
returns a plan whose `plan_digest` binds the archive hash, its classification,
the destination observation, every install-folder observation and the
selection. `restore_isolated()` refuses unless the caller passes that digest and
a fresh plan reproduces it. A restore without a selection takes every unit or
refuses: any damaged or dependency-blocked unit (including a legacy database
with pending WAL frames) requires an explicit owner selection, and only
independently valid units can be chosen. Install-folder files that differ
from the archive block the plan until the owner chooses to keep the
destination's file or skip the unit; identical files are left alone.

Data-folder contract: the destination must be absent or an empty directory.
Units are staged in a private sibling directory, SQLite copies are switched to
rollback-journal mode, Skills paths are rebound to the destination install, and
both databases must pass `PRAGMA integrity_check`. Archived triggers and views
are code, not history: before any write, every one is dropped except the
shipped Skills and chat-message search triggers, kept only when their stored
definition is exactly the shipped one. The Skills and chat search tables those
triggers write to must likewise be exactly the shipped definitions (a missing one
is created, as first start would); anything else refuses. Before publishing, the
engine replays on the staged database, and rolls back, what first start and
ordinary use do to it (search tables and triggers created if missing, a Skill and
a chat message written, changed and deleted, the Skills search index checked),
and refuses a database the runtime could not survive. Preferences pass the export
scrubber (credentials, PIN, owner/channel bindings and unknown keys dropped;
Telegram remote access off; `last_persona` rebound or dropped). No restored
Skill is official. The archived `skills` table must be exactly one of the
definitions released versions of Lumina created (its stored `CREATE TABLE` text,
whitespace aside), with only its own name index, or the restore refuses before
any authority decision: no other column, constraint, collation or index. The
first-start replay also writes a row as the official bootstrap does and proves
names are unique and matched under exact BINARY comparison (case and trailing
spaces both count). Only the `skills` table, its search table and the
chat-message search table are pinned to shipped definitions; other archived
tables (chats, chat messages, memories, Palace and the rest) are checked for
integrity and required columns, not for every constraint the runtime might
trip over. Four rules hold for every table: no archived column may be
generated; every column default must be a literal (NULL, a number or a quoted
string, as in every shipped table; an expression would be evaluated afresh on
each later insert); every foreign key and its referential actions must be one
the shipped runtime declares (the runtime enforces foreign keys, so any other
action, such as `ON DELETE SET DEFAULT`, would rewrite rows it wrote whenever it
deletes something); and each kind of row the runtime writes (memories, chats, chat
messages, Palace wings, rooms, drawers, closets and halls) is rehearsed on the
staged copy, where every column the runtime leaves unset must read back empty,
as in the shipped schema. An archived default therefore cannot stamp provenance
(a Palace drawer's trusted synthesis origin, which can make the runtime omit
conversation history; source links; admission or withheld marks) onto later
writes. These checks verify that an archive is a compatible, intact Lumina
backup; they are not a complete defence against a deliberately hostile
database. Within them, an archive can still restore a root where some ordinary
write fails later, and other SQLite schema features (for example conflict
clauses or column affinity) are not checked; the source install is untouched
either way. Rows already in the archive are restored as history, with the
layer, trust and provenance marks the archive carries: archived Palace entries
at the privileged layers are injected into context like the source's own, and
an archived compaction checkpoint keeps hiding the rows it covered. Historical
owner permissions, credentials, grants and C1 state are never restored, as
described above. Choosing an archive to restore is the owner's explicit
responsibility: a valid checksum shows the archive is intact, not where it
came from. Restore only a backup you made, or one whose source you trust. An archived official row named like one of the destination checkout's
tracked `skill_packages/official/` packages is set aside, so the checkout's own
first-start bootstrap reinstalls it from shipped bytes; every other one becomes a
user Skill. A user Skill whose name is exactly a shipped package's (which would
make that bootstrap stop at the package, skipping every later one) is kept under
a new name, `<name> (restored)` or `<name> (restored N)`, listed in the receipt as
`skills_renamed_for_bootstrap`. Archive hashes,
paths and local files vouch for nothing. The result is re-read before
publishing, and any row still claiming official refuses the restore. Custom tools, the tool audit log
(the custom-tool approval record), pending actions and project root bindings are
written only under `restore_history/<restore_id>/`, where nothing loads them.
The staged tree is validated against the plan (exact file set, hashes, no links,
no live authority filenames), install-folder files are created without ever
replacing one, and the data folder is published with one
`renameat2(RENAME_NOREPLACE)`. Before that rename the destination is untouched;
after it the folder is complete. Any earlier failure removes the staging folder
and every install file the restore created. Each one carries its restore id in
the `user.lumina.restore_id` extended attribute from the moment its temporary
file exists (an install folder that cannot hold it is refused), and its inode
and sealed size and modification time are journaled before it is linked into
place; a folder it creates is journaled before it exists. A file counts as the restore's only with that mark, inode and seal; equal
bytes or a reused inode are never proof. Anything else at the same path, or a
temporary file whose identity was not yet journaled, is left in place and
reported. Removal works through directory descriptors, so a folder swapped for a
link mid-recovery cannot redirect it. A killed restore leaves a journaled
staging folder that blocks new plans (the refusal names it) until
`recover_interrupted_restore()` rolls it back. Recovery reads only private
staging folders (real directories owned by this user, writable by no one else)
and acts only on a journal this engine could have written: a known state, the
same non-production install folder, targets inside the restore's own install
namespace (Skills, personas, avatars, voices, tool profiles, project documents)
and the engine's own temporary names. A staging folder killed before its first
journal write is removed. One that cannot be removed is reported and recovery
continues with the rest. Any other one is left intact and reported; the owner
can then call `recover_interrupted_restore(data_dir,
quarantine_unrecognized=True)`, which renames it aside as
`.<name>.restore-<id>.quarantined-<hex>` (never deleting it, or any install file
it names) so restores can proceed and the folder can be inspected. A process
running as the same user (including a tool-gated agent) could forge such a
private staging folder, but it can only reach files carrying a restore's own
ownership mark inside the install namespace, which that user could delete
anyway. A restore writes at most 1,000 install files (the journal is rewritten
for each one); a larger archive is refused when planning.
`verify_restored_root()` re-checks a published folder read-only,
including re-deriving the Skill authority and executable-schema invariants from
the published bytes.

C1 contract: archived C1 state is never written. Before publishing, the engine
calls `core.suspension_backup.write_restore_trace()`, which refuses any root that
already holds C1 state and writes a private, write-new-only receipt under
`memory/suspension_evidence/`. The published folder therefore probes
`absent_with_trace`: first-run Initialize is refused, and the owner must run
local Recover, which starts with a fresh identity, the owner suspended and
unknown scopes suspended. The trace grants nothing and needs no owner envelope.

Each restore writes `restore_history/<restore_id>/receipt.json` (archive
identity and class, coverage, plan digest, every written file with its hash,
rebinds, SQLite checks, the C1 trace and the authority fence) and a journal.
Nothing in the restored database is resumed: the background task queue is
in-memory only, restored media-generation jobs are consulted only to refuse a
duplicate submission, and pending actions never reach the live queue.

Not yet covered: restore into an existing or production data folder, writer
exclusion for a live destination, a rollback snapshot of an existing destination,
Knowledge Pack bootstrap reconciliation on first start, and a full desktop launch
of a restored folder. These remain required before any production
disaster-recovery claim.

## Settings cockpit (Recover Archive (Isolated))

Settings → Memory → **Recover Archive (Isolated)...** (local owner only) is a
front end for the isolated slice above. It adds no recovery capability and
relaxes no engine rule. Its decisions and wording are in the Qt-free
`core/agent_backup_restore_cockpit.py`; the dialog is
`ui/settings/restore_cockpit.py`.

1. **Inspect (read-only).** Choose a local ZIP. The dialog shows its SHA-256,
   size, format and verification status; the recovery class (Incomplete State
   Export recovery, Selective Recovery or Legacy Recovery, never a Verified Agent
   Backup); coverage, exclusions, unknown installation history, damaged units
   with their reasons, and compatibility limits. Every archive-derived string is
   displayed as plain text, with control, bidirectional-override and invisible
   characters shown as `?` (display only; the archive and anything recovered are
   never altered).
2. **Choose units.** Each recoverable unit takes *Recover*, *Recover, keep my
   differing install files* or *Skip*. Damaged and dependency-blocked units are
   listed with the reason and cannot be chosen. A clean archive starts on
   Recover; a selective archive, or any archive with a damaged unit, starts on
   Skip and recovers nothing until the owner picks something.
3. **Choose folders.** A new (or empty) data folder and a fresh install folder.
   Production roots, the running installation and this process's data folder are
   refused by the engine and reported as an unsafe destination.
4. **Preview.** `plan_restore()` runs off the GUI thread. The dialog lists every
   file with its action (create; already present and identical; differs, so your
   file is kept), install-file conflicts, the free space needed, the plan digest
   and what always holds. Nothing is written. A plan with undecided conflicts
   cannot be approved.
5. **Approve and recover.** Approval is bound to the displayed `plan_digest`.
   Changing the archive, any unit or either folder drops the plan and the
   approval. Recovering calls `restore_isolated()` with that digest and then
   `verify_restored_root()`; the outcome is a success only if both pass. A folder
   that was published but fails verification is reported as such, never as
   success. One attempt consumes the plan and the approval whether it succeeds or
   fails, so a retry needs a new preview and a new approval. An archive whose
   bytes changed since it was inspected is named as such (by SHA-256) and forces
   a new inspection. Nothing is retried, widened or adjusted automatically. If
   the engine raises after publishing (a late sync, say), the dialog reads the
   stored receipt, verifies the folder and says exactly that.
6. **Receipt.** `restore_history/<id>/receipt.json` inside the new folder is the
   durable receipt. The dialog shows its path and can copy the path, show the
   receipt read-only, or save a copy to a new file (never overwriting).
   *Verify a recovered folder* re-checks an earlier recovery against the receipt
   stored in it; that detects later changes, not a receipt replaced together with
   the files.
7. **Failures** are told apart as unsafe destination, invalid archive,
   incomplete coverage or selection, interrupted recovery pending, stale plan,
   insufficient resources, engine failure, published but unverified and
   unexpected error, each with the engine's rollback report where it has one.
   *Check for interrupted recovery* runs `recover_interrupted_restore()` after a
   confirmation; unrecognised staging folders are left untouched until the owner
   separately chooses to move them aside (never deleted).

Threads and closing. Every engine call runs in a worker thread that the
application retains, as the Agent Backup exporter does; none runs on the GUI
thread. The engine has no cancellation and none is offered. The dialog cannot be
closed while a recovery or an interrupted-recovery step runs, normal application
quit waits for it, and a read-only step that is still running when the dialog
closes finishes unseen.

A recovered Knowledge Pack installation record is not inert. It is restored to
`knowledge_packs_installed.json` in the new data folder, the file Lumina's
startup reads to decide whether the official pack is installed again, so it is
historical installation state that can influence that decision if Lumina is
later started on the folder. It grants no authority and no official-package
trust; reconciling it before any launch is part of the pending work below.

What this is not. It does not activate the recovered folder, switch Lumina to
it, stop Lumina, offer "Restore Current Lumina", or take a rollback snapshot.
Starting Lumina on a recovered folder is not a supported or verified path until
the "Not yet covered" items above, C1 owner-local Recover, Knowledge Pack
reconciliation and project rebind have landed. A passing UI test suite is not a
disaster-recovery claim.
