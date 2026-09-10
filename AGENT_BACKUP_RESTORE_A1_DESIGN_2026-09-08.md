# AGENT-BACKUP-RESTORE-A1
## Canonical State Ownership & Backup Manifest Design

**Status:** FINAL. DESIGN / SOURCE-VET ONLY — no production code was modified to produce this document, and none of the amendments below required any. No commit, push, or release→dev mirror has occurred. A2 (Agent Backup collection) is cleared to begin.

**Amendment log (post-review, same day):** Sol (architecture/planning) reviewed the first draft and returned four corrections/decisions, all applied in place below rather than left as a diff:
1. Section A's ancestry receipt named the wrong release commit (`26f1dd6`, a stale checkpoint) as the one confirmed ancestor of dev — corrected to the actual A1-relevant release HEAD, `ee40259`, independently re-verified via `git merge-base --is-ancestor` before editing.
2. Section C's custom-tool/tool-audit-log rows said plain "overwrite," contradicting this document's own Forward Notice that restored approval claims must not become automatic authority — resolved into one explicit canonical rule (Section C, just above the atomicity note): content is REQUIRED and always restored, executable approval authority is always quarantined/revalidated post-restore, never inherited from the archive.
3. `ledger.db`'s classification is now decided, not merely recommended: REBUILDABLE_GENERATED, always regenerated empty, stale entries never restored.
4. `identity_trailers.json`'s fate is now decided, not left to the owner as an open item: kept, included as optional non-required operator metadata (tagged `non_runtime_authority`), never treated as a secret, never gating Agent recovery.

The one item that remains genuinely open (confirm `projects/*/chats.json`'s on-disk location) is unchanged, non-blocking A2 homework, per Section L / Entry Criteria.

**Second amendment log (same-day schema-consistency pass):** Sol reviewed the amended draft and found the architecture sound but flagged four internal contract mismatches — document fixes, not another design pass:
1. Section G said every `USER_OVERLAY` member must be `required: true`, directly contradicting `identity_trailers.json`'s own `required: false`. Resolved by clarifying what `required` actually means (required for Agent recovery, not "belongs to an important state class") and stating the real per-class rule, including that USER_OVERLAY legitimately splits both ways.
2. `provenance: "operator_authored"` was used on the `identity_trailers.json` manifest example but was missing from the Section B/J provenance enum. Added it to both, with its own short explanation in Section J alongside the `retained_artifact` case.
3. `ledger.db` was still modeled as a real `members` entry (with `archive_path`/`sha256`/`size`) even though Sections C/L decided its content is never archived at all. Split into a new top-level `regenerate` manifest array (Section E/F/G), removed from the archive tree, and Section M's "decide on ledger.db" language corrected to reflect that the decision is already made.
4. `format_version` was a bare integer even though Section H's own prose already talked about major/minor semantics it couldn't express. Changed to `{"major": 1, "minor": 0}` throughout the manifest example and compatibility rules (Section E/H).

All four are applied in place below; no contradictory remnants of the old integer `format_version`, the ledger.db-as-member shape, the missing `operator_authored` value, or the over-strict `required` rule remain.

**Note on inputs:** The referenced `AGENT-BACKUP-RESTORE-A0` report (durable-state inventory & restore recon) could not be located as a file in `~/lumina-release`, `~/lumina`, the handoffs/patches storage directories, memory, or prior session transcripts — it appears to exist only as a prior conversation's chat output that was never persisted. Rather than blocking on it, this document re-derives A0's findings from live source (per this campaign's own Section 1 mandate to source-vet fresh, and its instruction to only "repeat A0 from scratch" if fresh source proves the summary stale). Every specific claim below — including the ones this campaign's own brief already asserted as A0 findings (personas outside `DATA_DIR`, Flight Recorder traversed into the archive, `ledger.db` not checkpointed, credentials excluded by design, etc.) — was independently re-verified against the current `~/lumina-release` checkout and is cited by file:line. All of A0's asserted findings were confirmed accurate against current source; none were found stale.

---

## A. Live Baseline

### Release — `~/lumina-release`

```
branch:        main
HEAD:          ee40259c980d3c6fcca56fc2f27dcefd3814c178
origin/main:   ee40259c980d3c6fcca56fc2f27dcefd3814c178   (HEAD == origin/main, fully pushed)
last commit:   ee40259 "docs: refresh README and add persona assets"
status:        clean (no staged/unstaged/untracked changes)
merge/rebase state: none (no MERGE_HEAD, REBASE_HEAD, CHERRY_PICK_HEAD, BISECT_LOG)
```

### Dev — `~/lumina`

```
branch:        dev-local
HEAD:          9b09542489210445b6b7fcc4ef2148e5f9eb64a8  ("Merge release: README and persona assets")
ancestry:      release HEAD ee40259 (the actual A1-relevant release commit — the earlier draft of
               this document erroneously reported the older 26f1dd6 checkpoint here) confirmed an
               ancestor of dev-local HEAD via `git merge-base --is-ancestor ee40259 HEAD` (release
               lineage fully intact, current through the exact commit this campaign ran against)
remote:        release-local -> /home/bino/lumina-release (fetch only)
               release-local pushurl = DISABLED
               push.default = nothing
pre-push hook: present and blocking ("Push blocked: ~/lumina is a local-only development repository.")
merge/rebase state: none
status:        1 modified (projects/lumina-dev/codebase.md — a generated codebase-index file, expected drift,
                see Section C), 4 untracked skills/*.md files (authorized-security-operations.md,
                kali-operating-modes.md, kali-study-and-lab-workflow.md, release-dev-mirror-protocol.md —
                in-progress work from an unrelated thread; per established convention this is routine
                untracked storage, not a dirty-tree blocker, and is left untouched by this campaign)
```

Neither tree was modified during this source-vet. No hidden git operation state in either repo.

---

## B. Canonical State Taxonomy

Five mutually-exclusive **state classes** (every backup member belongs to exactly one), plus one cross-cutting **provenance** tag (Section J) that can apply to a member regardless of its class.

| Class | Definition | Restore-time treatment |
|---|---|---|
| **REQUIRED_AGENT_STATE** | Durable state without which "this Lumina" — its memory, identity, configuration, and history — is measurably incomplete. No current code path can regenerate it. | Always restored, verified by hash. |
| **USER_OVERLAY** | User- or Lumina-created content that lives beside (and is indistinguishable in storage terms from) shipped product files under the same directory. | Restored with conflict/provenance awareness — never blindly overwrites a fresh install's shipped baseline (Section D). |
| **REBUILDABLE_GENERATED** | Derivable from other REQUIRED/USER_OVERLAY state by a function the product already has (or trivially could have). Archiving it is an optimization, not a requirement. | Either omitted, or archived for speed and explicitly marked stale/to-be-regenerated after Restore. |
| **REBIND_REQUIRED** | Durable and worth preserving, but contains machine-local identity (an absolute path, a filesystem binding, a device/service endpoint) that cannot be trusted literally on a different machine, install location, or after a fresh reinstall. | Content restored; the machine-local fields are held **not-yet-valid** until the owner confirms or re-supplies them. |
| **EXCLUDED** | Deliberately outside Agent Backup — either a hard security boundary (credentials) or ephemeral/process-local/scratch state with no cross-session meaning. | Never touched by Backup or Restore, in either direction. |

**Provenance** (cross-cutting, not a class): `shipped_baseline_unmodified`, `shipped_baseline_modified`, `user_created`, `lumina_created`, `imported`, `unknown`, `retained_artifact` (unusual origin, intentionally kept — the "rubber duck" case, Section J), `operator_authored` (human/operator-created engineering metadata, not Agent-facing content at all — `identity_trailers.json` is the one instance so far, Section C). A member can be e.g. `state_class=USER_OVERLAY, provenance=retained_artifact` simultaneously — class governs restore mechanics, provenance governs how Restore should *talk about* the item to the owner (Section 22-style preview), never whether to silently delete it.

---

## C. State Ownership Matrix

`DATA_DIR` resolves via `platformdirs.user_data_dir("lumina")` or `LUMINA_DATA_DIR` override ([config.py:69](config.py:69)); `BASE_DIR` is the installed application root ([config.py:59](config.py:59)). The **existing** `core/memory_backup.py` (`build_memory_backup`, [core/memory_backup.py:31-62](core/memory_backup.py:31)) only WAL-checkpoints `lumina.db` + Flight Recorder, then `os.walk(DATA_DIR)` — i.e. it captures every `DATA_DIR` row below and **none** of the `BASE_DIR` rows.

| State item | Class | Ownership | Backup policy | Restore policy | Portability | Rebind/Rebuild |
|---|---|---|---|---|---|---|
| Chat history (`chats`, `chat_messages`, `chat_messages_fts`) — lumina.db | REQUIRED | Product schema / user content | Include (whole `lumina.db` as one atomic unit — see note below) | Overwrite/atomic restore | PORTABLE | none |
| MemPalace L0–L3 (`palace_wings/rooms/closets/drawers/halls`) — lumina.db | REQUIRED | User/Lumina content | Include | Overwrite (atomic w/ lumina.db) | PORTABLE | none |
| Flat `memories` table (legacy fallback) — lumina.db | REQUIRED | User/Lumina content | Include | Overwrite (atomic w/ lumina.db) | PORTABLE | none |
| Knowledge Base (`knowledge`, `people`) — lumina.db | REQUIRED | User/Lumina content | Include | Overwrite (atomic w/ lumina.db) | PORTABLE | none |
| My Human profile (`prefs.json`: `human_bio`, `human_profile_curated`, `human_bio_public`) | REQUIRED | Owner + Lumina-curated | Include | Overwrite | PORTABLE | none |
| Preferences (`prefs.json`, remainder) | REQUIRED | User settings | Include | Overwrite, but see identity-vs-machine split below | PORTABLE (mostly) | `last_persona` field is an **absolute path** — see Section D/I |
| Custom tools (`DATA_DIR/custom_tools/*.py`, `_pending/*.py`) | REQUIRED | Lumina/user-created | Include | Restore content verbatim; **quarantine on the destination** — do not let a restored `.py` file become live/callable until the approval state below is revalidated | PORTABLE_WITH_REBIND | Executable authority must be re-earned post-restore, not inherited from the archive (see canonical rule below) |
| Tool audit log (`DATA_DIR/memory/tool_audit.log`) | REQUIRED — **load-bearing**, not forensic-only: `load_approved_custom_tools()` replays it at startup ([tools/toolmaker.py:166-232](tools/toolmaker.py:166)) | System-generated | Include | Restore log content verbatim (preserves full approve/reject history) — but see canonical rule below: Restore must not let a straight replay of this restored log auto-grant executable approval on the destination machine | PORTABLE_WITH_REBIND | Same as above — content survives, authority does not silently survive an untrusted archive |
| Pending-actions queue + audit log (`pending_actions.json`, `pending_actions_audit.log`) | REQUIRED | System-generated | Include | Overwrite | PORTABLE | none |
| Coding checkpoints (`coding_checkpoints`) | REQUIRED, historical | User/Lumina content, project-scoped | Include | Retain as history; **not** authoritative until revalidated against (possibly rebound) project target | PORTABLE_WITH_REBIND | Revalidate against project root after any project rebind |
| Coding validation evidence (`coding_validation_evidence`) | REQUIRED, historical | System-generated | Include | Same as above — "machine evidence retained as history, not automatically authority" | PORTABLE_WITH_REBIND | Same |
| Context checkpoints (`context_checkpoints`, chat-scoped) | REQUIRED | System-generated | Include | Overwrite (atomic w/ lumina.db; FK-consistent with `chats`) | PORTABLE | none |
| Flight Recorder (`DATA_DIR/telemetry/flight_recorder.db`) | REQUIRED — diagnostic history | System-generated | Include (own WAL checkpoint via `flight_recorder.checkpoint()`, already implemented [core/flight_recorder.py:372-383](core/flight_recorder.py:372)) | Retain as history; internal `runtime_id`/`process_id`/`worktree_id` fields are labels on past events, not live pointers — no rebind action needed, just don't treat as current-process identity | PORTABLE | none (labels, not live refs) |
| Idempotency ledger (`DATA_DIR/memory/ledger.db`) | REBUILDABLE_GENERATED — **decided, not provisional** | System-generated, 24h-TTL dedupe cache | **Not archived at all** — recorded only as a `regenerate` entry in the manifest (Section E/G), never a `members` entry, never a hash, never a byte of its content read | **Always regenerate empty. Never restore stale ledger rows** — a restored old entry only risks wrongly suppressing a legitimate retry, and buys nothing since its own TTL means any entry old enough to matter for a backup has already expired anyway | REBUILD | none |
| Personas (`BASE_DIR/personas/*.json`) | USER_OVERLAY | Mixed shipped + user-created | Include (currently **not** captured — gap) | Baseline-hash-aware merge, never blind overwrite (Section D) | PORTABLE_WITH_REBIND (see avatar/last_persona note) | `prefs.json["last_persona"]` stores an **absolute path** to a persona file — must be rewritten to the destination `BASE_DIR/personas/...` post-restore, or the current fallback (`os.path.exists()` fails silently → no persona loads) fires |
| Persona avatars (`BASE_DIR/assets/avatars/*.png`) | USER_OVERLAY | Mixed shipped + user-created | Include (currently not captured — gap) | Baseline-hash-aware merge | PORTABLE | Persona JSON already stores avatar refs as `BASE_DIR`-relative text (e.g. `"assets/avatars/LUMINA - avatar.png"`) — portable as long as Restore always redeploys to a `BASE_DIR/assets/avatars/` layout |
| Voice reference clips (`BASE_DIR/assets/voices/*.wav`) | USER_OVERLAY | Mixed shipped + user-addable | Include (currently not captured — gap) | Baseline-hash-aware merge | PORTABLE | none |
| Skills markdown (`BASE_DIR/skills/*.md`) | USER_OVERLAY | Mixed shipped fixtures + Lumina-created + one eval-artifact | Include (currently not captured — gap) | Baseline-hash-aware merge | PORTABLE | none (content itself has no embedded paths) |
| Skills DB index (`skills`, `skills_fts` — lumina.db) | REQUIRED (see Known Limitation L) | System-generated pointer + metadata | Include (part of atomic lumina.db) | Overwrite, **then rewrite `skills.path` column** | PORTABLE_WITH_REBIND | `path` column holds an **absolute, `BASE_DIR`-anchored** path baked in at insert time ([core/skills.py:35-37](core/skills.py:35)) — restoring lumina.db onto a different install location leaves rows pointing at the wrong filesystem location even though the `.md` files themselves restore correctly. No code currently rewrites this column — **A2/A3 must add that step.** |
| Tool profiles (`BASE_DIR/tool_profiles/*.json`) | USER_OVERLAY (provisional — no runtime creation path was found; may be closer to PRODUCT_BASELINE in practice) | Shipped defaults; only one has ever been edited, and that edit was a source commit, not a runtime UI action | Include (currently not captured — gap) | Baseline-hash-aware merge | PORTABLE | none |
| Projects — portable docs (`BASE_DIR/projects/projectlist.md`, `<name>/project.md`) | USER_OVERLAY / REQUIRED | User-authored identity/description/notes | Include (currently not captured — gap) | Baseline-hash-aware merge (projectlist.md), direct restore (project.md, no shipped baseline exists for user projects) | PORTABLE | none |
| Projects — generated codebase index (`<name>/codebase.md`) | REBUILDABLE_GENERATED | System-generated (`refresh_codebase_index()`, [tools/projects.py:178-234](tools/projects.py:178)) | Archive for convenience (cheap), but always mark stale and recommend regeneration after Restore | REBUILD | Requires the bound project root to exist and be revalidated first |
| Projects — machine binding (`DATA_DIR/projects/<name>/binding.json`) | REBIND_REQUIRED | System-generated | Include (already captured — this one's design is already correct) | Content restored; root path must be validated to exist / re-picked before use, never trusted literally | PORTABLE_WITH_REBIND | Owner must confirm/re-pick project root |
| Projects — per-project chat-linkage cache (`projects/*/chats.json`) | Unconfirmed — flagged open item | Unconfirmed | Defer classification to A2 | Defer | Unconfirmed | Needs a follow-up grep; neither source-vet pass nailed its exact on-disk directory |
| Scheduled/background tasks (in-process heap, [core/task_queue.py:25-27](core/task_queue.py:25)) | EXCLUDED — **known product limitation, not a design gap** | System-generated | Cannot be included — no disk persistence exists in the product today | N/A | EXCLUDED | N/A — see Section L |
| Guardrails denylist (`tools/guardrails.py`) | N/A — source code, not state | Product code | Out of scope entirely (ships with the app; not user/session state) | N/A | N/A | N/A |
| Identity trailers config (`~/.config/lumina/identity_trailers.json`) | USER_OVERLAY — **decided**: include, optional, `non_runtime_authority` | Operator/engineering-authored (AIvengers attribution record) — **zero code consumers found** (`grep -rn "identity_trailers"` across the whole tree returns nothing), so it is genuinely not runtime Agent state today | Include as optional operator metadata (`metadata/identity_trailers.json`) in full disaster-recovery backups | Restore verbatim if present; never required, never gates or blocks Agent recovery, and — critically — never treated as credentials (no secrets in it, no `0600` boundary applies) | PORTABLE | none — kept for its own sake, not rebound to anything |
| Credentials (`~/.config/lumina/credentials.json`) | EXCLUDED_BY_DESIGN | Security boundary | Never included, by design, already documented in code ([core/memory_backup.py:33-34](core/memory_backup.py:33)) | Never touched by Restore in either direction | EXCLUDED | N/A |
| Ephemeral coding worktrees (`DATA_DIR/worktrees/`) | EXCLUDED | Process/session-ephemeral git working copies | Exclude | N/A | EXCLUDED | N/A |
| Scratch/tmp (`BASE_DIR/memory/sandbox_tmp/`, `browser_screenshots/`, eval harness `eval/`) | EXCLUDED | Dev-tooling / scratch | Exclude | N/A | EXCLUDED | N/A |

**Canonical rule — custom-tool approval (resolves an internal inconsistency in the first draft of this document):** the first draft listed custom tools and the tool audit log as plain "overwrite," which contradicted this same document's own Forward Notice that restored approval claims must never become automatic authority. The corrected, single rule: **backup the custom-tool `.py` source and the full approval/audit history unconditionally — that content is REQUIRED and always restored. But Restore must quarantine every restored custom tool and treat the replayed audit log as historical evidence only, requiring an explicit revalidation/re-approval gate before `load_approved_custom_tools()` (or its Restore-time equivalent) treats anything from the archive as live-approved on the destination machine.** Content survives an untrusted archive; executable authority does not silently survive one.

**Atomicity note:** `lumina.db` is restored as a single physical file, never as a partial set of tables. Its FTS5 virtual tables (`chat_messages_fts`, `skills_fts`) are not separate archive members and need no independent backup treatment — they travel for free inside the same file and already have a built-in `INSERT INTO fts(fts) VALUES('rebuild')` safety valve if a restored index is ever found inconsistent with its content table.

---

## D. Product/User Overlay Rules

The mixed-ownership problem (personas, avatars, Skills, Projects, tool profiles, voices — all `BASE_DIR`-relative, all mixing shipped defaults with user/Lumina-created content, none captured by the current backup) is solved the same way for every one of these categories, rather than inventing a bespoke rule per directory:

### The mechanism: a bundled shipped-baseline hash manifest

Each Lumina release build ships a small reference file — e.g. `metadata/shipped_baseline_hashes.json`, generated once per release version, committed to the release repo — listing the sha256 of every file the product ships by default under `personas/`, `assets/avatars/`, `assets/voices/`, `skills/`, `tool_profiles/`, `projects/projectlist.md`. At backup time, the tool hashes the live file and compares:

- **Hash matches known shipped baseline** → `provenance: shipped_baseline_unmodified`. Safe to skip archiving the content itself (reference by version + filename is enough) or archive it anyway for simplicity — either is correct; recommend archiving anyway since these files are small and it keeps Restore's logic uniform (always restore what's in the archive, never conditionally "reach back" to a version's shipped defaults).
- **File exists in the baseline list but hash differs** → `provenance: shipped_baseline_modified`. User has edited a shipped file (e.g. hand-edited `personas/lumina.json`). Archive it; Restore must never silently clobber this with a fresh install's shipped version.
- **File not in the baseline list at all** → `provenance: user_created` or `lumina_created` (disambiguated by any available secondary signal — see Skills below). Always archived, always restored.

This one mechanism replaces the need to retroactively add a `"source"`/`"origin"` field to every JSON schema (personas, tool profiles) — a real option worth considering for a future product change, but not required to make Agent Backup correct today.

One existing signal is worth noting but is **not sufficient alone**: persona JSON already has a `"protected": true/false` field ([personas/lumina.json](personas/lumina.json)). This currently gates UI delete-ability, not backup/restore ownership — `mara_voss.json`/`mr_robot.json` are shipped example personas with `protected: false`, so `protected` cannot be read as "is this shipped." The hash-manifest mechanism above is the actual source of truth; `protected` is orthogonal UI behavior, unaffected by this design.

### Per-category specifics

- **Personas + avatars**: travel as a unit (evidenced by the [ee40259](https://github.com/Bino5150/lumina/commit/ee40259) commit adding both together). Archive both under the mechanism above. The one real rebind concern is not the avatar reference itself (already `BASE_DIR`-relative portable text) but `prefs.json["last_persona"]`, which stores an **absolute path** to the currently-selected persona file (Section I).
- **Skills**: the `.md` files are the payload; `core/skills.py`'s DB rows are a pointer + metadata index, not a duplicate copy of content ([core/skills.py:56-88](core/skills.py:56)). Distinguish shipped fixtures (5 files, all present at the release's initial commit, including a self-evidently meta fixture, `test-skill-feature-verification.md`) from Lumina-created ones (e.g. `save-technical-memory-before-context-limits.md`, `temporal-decay-engine-implementation.md`) via the hash-manifest mechanism. One file, `rubber-duck-debugging.md`, is a **retained artifact** (Section J) — real content, genuinely created by a tool call during an eval run, deliberately excluded from git via `.gitignore:39` yet physically present and in active use by the running Skills system. It is not in the shipped baseline (so the hash mechanism correctly tags it `unknown`/not-shipped) and must be archived like any other user/Lumina-created skill — its unusual origin is not a reason to exclude it.
- **Tool profiles**: same mechanism applies. No runtime UI path for creating a new tool profile was found in this pass (only persona-level *selection* of an existing named profile) — flagged as an open item for the product owner (Section L), not a blocker for archiving them uniformly with everything else in this section.
- **Projects — portable docs**: `project.md`/`projectlist.md` are genuinely user-authored (no shipped-baseline concept applies to a project the owner created; the mechanism above only matters for `projectlist.md`'s initial shipped scaffold). `codebase.md` is REBUILDABLE_GENERATED (Section C) and is handled separately from the overlay-ownership problem entirely.
- **Voices**: same mechanism; `assets/voices/lumina.wav` is the one shipped default today, but the directory is a valid target for user-added reference clips for other personas.

---

## E. Proposed `manifest.json` Schema (v1)

```json
{
  "format": "lumina-agent-backup",
  "format_version": { "major": 1, "minor": 0 },
  "lumina_version": "0.9.x-ee40259",
  "created_at": "2026-09-08T15:12:00Z",
  "source_platform": { "os": "linux", "python": "3.13" },
  "source_data_root": {
    "note": "informational only — never trusted literally by Restore",
    "data_dir": "/home/bino/.local/share/lumina",
    "base_dir": "/home/bino/lumina-release"
  },
  "backup_mode": "full",
  "compatibility": {
    "min_supported_format_version": { "major": 1, "minor": 0 },
    "max_known_format_version": { "major": 1, "minor": 0 },
    "unknown_newer_major_policy": "refuse",
    "unknown_newer_minor_policy": "accept_ignore_unknown_optional_fields",
    "older_same_major_policy": "supported_with_migration_if_minor_delta",
    "older_major_mismatch_policy": "refuse_direct_to_explicit_migration_tool"
  },
  "hash_algorithm": "sha256",
  "members": [
    {
      "logical_id": "state.databases.lumina_db",
      "state_class": "REQUIRED_AGENT_STATE",
      "content_kind": "sqlite_database",
      "archive_path": "state/databases/lumina.db",
      "source_role": "primary_db",
      "sha256": "<hex64>",
      "size": 18874368,
      "required": true,
      "portable": "PORTABLE",
      "restore_policy": "overwrite_atomic",
      "rebind_policy": "rewrite_skills_path_column_to_dest_base_dir",
      "generated": false,
      "database_metadata": {
        "sqlite_version": "3.45.x",
        "user_version_pragma": 0,
        "journal_mode": "wal",
        "integrity_check": "ok",
        "tables": ["chats", "chat_messages", "chat_messages_fts", "memories", "knowledge",
                   "people", "palace_wings", "palace_rooms", "palace_closets", "palace_drawers",
                   "palace_halls", "skills", "skills_fts", "coding_checkpoints",
                   "coding_validation_evidence", "context_checkpoints"]
      }
    },
    {
      "logical_id": "state.telemetry.flight_recorder_db",
      "state_class": "REQUIRED_AGENT_STATE",
      "content_kind": "sqlite_database",
      "archive_path": "state/telemetry/flight_recorder.db",
      "sha256": "<hex64>",
      "size": 4108288,
      "required": true,
      "portable": "PORTABLE",
      "restore_policy": "overwrite_atomic",
      "generated": false,
      "database_metadata": {
        "retention_policy_at_backup_time": {
          "full_retention_seconds": 604800,
          "error_retention_seconds": 7776000,
          "max_db_bytes": 524288000
        }
      }
    },
    {
      "logical_id": "state.preferences.prefs_json",
      "state_class": "REQUIRED_AGENT_STATE",
      "content_kind": "json",
      "archive_path": "state/preferences/prefs.json",
      "sha256": "<hex64>",
      "size": 7246,
      "required": true,
      "portable": "PORTABLE_WITH_REBIND",
      "rebind_policy": "rewrite_last_persona_absolute_path_to_dest_personas_dir",
      "generated": false
    },
    {
      "logical_id": "overlay.personas.lumina",
      "state_class": "USER_OVERLAY",
      "content_kind": "json",
      "archive_path": "overlay/personas/lumina.json",
      "sha256": "<hex64>",
      "size": 612,
      "required": true,
      "portable": "PORTABLE",
      "provenance": "shipped_baseline_unmodified",
      "restore_policy": "baseline_aware_merge"
    },
    {
      "logical_id": "overlay.skills.rubber_duck_debugging",
      "state_class": "USER_OVERLAY",
      "content_kind": "markdown",
      "archive_path": "overlay/skills/rubber-duck-debugging.md",
      "sha256": "<hex64>",
      "size": 941,
      "required": true,
      "portable": "PORTABLE",
      "provenance": "retained_artifact",
      "restore_policy": "baseline_aware_merge"
    },
    {
      "logical_id": "overlay.projects.lumina.codebase_md",
      "state_class": "REBUILDABLE_GENERATED",
      "content_kind": "markdown",
      "archive_path": "overlay/projects/lumina/codebase.md",
      "sha256": "<hex64>",
      "size": 9800,
      "required": false,
      "portable": "REBUILD",
      "restore_policy": "restore_then_mark_stale_recommend_regenerate"
    },
    {
      "logical_id": "metadata.identity_trailers",
      "state_class": "USER_OVERLAY",
      "content_kind": "json",
      "archive_path": "metadata/identity_trailers.json",
      "sha256": "<hex64>",
      "size": 1551,
      "required": false,
      "portable": "PORTABLE",
      "provenance": "operator_authored",
      "non_runtime_authority": true,
      "restore_policy": "restore_verbatim_if_present_never_required_never_credentials"
    },
    {
      "logical_id": "overlay.projects.lumina.binding_json",
      "state_class": "REBIND_REQUIRED",
      "content_kind": "json",
      "archive_path": "overlay/projects/lumina/binding.json",
      "sha256": "<hex64>",
      "size": 88,
      "required": true,
      "portable": "PORTABLE_WITH_REBIND",
      "restore_policy": "restore_then_require_owner_confirmation_of_root_path"
    }
  ],
  "regenerate": [
    {
      "logical_id": "state.databases.ledger_db",
      "state_class": "REBUILDABLE_GENERATED",
      "restore_policy": "always_regenerate_empty",
      "note": "24h idempotency/dedupe cache. No archive_path, no sha256, no size — its content is deliberately never read into the archive at all, not even for speed. This is a distinct shape from an ordinary `members` entry with required:false: those restore real archived bytes optionally; a `regenerate` entry restores nothing and simply records that this logical piece of state exists and must be recreated empty."
    }
  ],
  "exclusions": [
    { "logical_id": "credentials", "reason": "excluded_by_design", "note": "security boundary; never read, written, or referenced by Backup or Restore" },
    { "logical_id": "scheduled_tasks", "reason": "known_product_limitation", "note": "process-local in-memory heap; the product itself does not persist this today" },
    { "logical_id": "worktrees", "reason": "ephemeral", "note": "session-local coding-agent git working copies" },
    { "logical_id": "scratch_tmp", "reason": "ephemeral", "note": "sandbox_tmp, browser_screenshots, eval harness fixtures" }
  ],
  "warnings": [
    "projects/*/chats.json per-project linkage cache location not fully confirmed — deferred to A2"
  ]
}
```

---

## F. Logical Archive Layout

```
manifest.json

state/
  databases/
    lumina.db                      (ledger.db is deliberately NOT here — see `regenerate` in
                                     manifest.json; its content is never archived, only its
                                     existence-and-regenerate-empty policy is recorded)
  telemetry/
    flight_recorder.db
  preferences/
    prefs.json
  custom_tools/
    <name>.py ...
    _pending/<name>.py ...
  audit/
    tool_audit.log
    pending_actions.json
    pending_actions_audit.log

overlay/
  personas/
    <name>.json ...
  avatars/
    <file>.png ...
  voices/
    <file>.wav ...
  skills/
    <file>.md ...
  tool_profiles/
    <file>.json ...
  projects/
    projectlist.md
    <project-name>/
      project.md
      codebase.md                  (optional/REBUILD)
      binding.json                 (REBIND_REQUIRED)

metadata/
  shipped_baseline_hashes.json     (bundled per Lumina version, used for provenance diffing)
  identity_trailers.json           (optional, operator metadata, non_runtime_authority — decided
                                     included, never gates Agent recovery, never treated as a secret)
```

Rules: no absolute source-machine paths appear anywhere as archive member names; no `../`; every logical item has exactly one archive identity regardless of which physical directory (`DATA_DIR` vs `BASE_DIR` vs `~/.config/lumina`) it was actually read from on the source machine. `binding.json` is a good illustration of the Section 7 design law in action: its *source* lives under `DATA_DIR/projects/<name>/binding.json`, but its *logical* home in the archive is `overlay/projects/<name>/binding.json`, grouped with the rest of that project's identity rather than with the database-adjacent state it happens to be filed next to on disk today.

---

## G. Per-Member Metadata

**Required fields, every member:** `logical_id`, `state_class`, `content_kind`, `archive_path`, `sha256`, `size`, `required` (bool), `portable` (enum, Section H below), `restore_policy`.

**Optional fields, as applicable:** `source_role`, `rebind_policy`, `generated` (bool), `provenance`, `schema_version`, `database_metadata` (for sqlite members: `sqlite_version`, `user_version_pragma`, `journal_mode`, `integrity_check`, `tables`).

**`regenerate` is a separate top-level manifest array, not part of `members`.** An entry there (`logical_id`, `state_class`, `restore_policy`, optional `note`) carries none of the "required fields, every member" list above — no `archive_path`, no `sha256`, no `size` — because nothing was archived: the whole point is that this piece of state's content was never read into the archive at all. `ledger.db` (Section E/F) is the one instance today. Do not model this as a `members` entry with `required: false`; that shape says "here is optional archived content," which is a different claim from "nothing here was archived, regenerate it empty."

**`required` means required for successful Agent recovery — not "belongs to an important state class."** The two axes usually line up but are not the same thing, and collapsing them was an inconsistency in the first draft (it declared `identity_trailers.json` both `USER_OVERLAY` and `required: false` in the same breath it said every `USER_OVERLAY` member must be `required: true`). Corrected rule, by class:

- Every REQUIRED_AGENT_STATE and REBIND_REQUIRED member is `required: true` — recovery is incomplete without it, even where a rebind step must run before the content is trusted.
- Every REBUILDABLE_GENERATED member is `required: false` by definition — recovery doesn't need it, at most convenience or historical continuity.
- USER_OVERLAY genuinely splits both ways: most overlay content (personas, Skills, avatars, voices, tool profiles, portable project docs) is `required: true` — losing it is a real, measurable loss of "this Lumina" even though it isn't REQUIRED_AGENT_STATE. But content that is USER_OVERLAY only in the ownership sense (operator/human-authored, outside `DATA_DIR`/`BASE_DIR`, no runtime Agent dependency) and is not itself needed for Agent recovery is correctly `required: false` — `identity_trailers.json` (Section C) is the concrete instance, and manifest members carrying `non_runtime_authority: true` are exactly the ones expected to use this combination.
- EXCLUDED items never appear in `members` at all; they appear only in the `exclusions` block (Section K), which is deliberately shaped differently so Restore never confuses "excluded by design" with "missing/corrupt." REBUILDABLE_GENERATED members whose content must never be read into the archive at all (not even for speed) belong in a third, separate `regenerate` block instead of `members` — see the `ledger.db` correction in Section E/F/M below; a "don't restore this content" item is not the same shape as "here is this content, restored" with `required: false`.

---

## H. Version/Compatibility Model

Portability enum (used in the `portable` field above and throughout):

| Value | Meaning |
|---|---|
| `PORTABLE` | Restores directly onto a compatible healthy installation, no owner action needed. |
| `PORTABLE_WITH_REBIND` | Content restores; a machine-local field inside it must be validated or re-supplied by the owner before it's trusted (Section I). |
| `SAME_MACHINE_ONLY` | Retained as history; applicability does not transfer to a new machine/install. |
| `REBUILD` | Restore canonical inputs, then regenerate this member. |
| `EXCLUDED` | Not part of Agent Backup at all. |

`format_version` is a `{major, minor}` pair (Section E), not a single integer — a single integer cannot express "older, same major" versus "older, minor delta" as two distinct, differently-handled cases, which the first draft's schema silently failed to represent even though its own prose already talked about major/minor. Compatibility rules for a future Restore to check before activating any archive:

- **`major` newer than what Restore understands** → refuse, unconditionally. Never attempt best-effort parsing of an unknown-newer major format.
- **`major` matches, `minor` newer** → accept; unknown optional fields that minor version introduced are ignored (see below), never fatal.
- **`major` matches, `minor` older** → accept, with migration if a minor schema delta exists (e.g. a field renamed) between the archive's minor and what Restore natively expects.
- **`major` older (mismatch)** → refuse, direct the owner to an explicit migration tool (not built here).
- **Unknown optional field in a member or in manifest root** → ignored, logged as a warning, never fatal — this is what lets a newer Lumina version add fields without breaking an older Restore reading its own past backups.
- **Unknown required state class** (a class name Restore has never heard of) → refuse that member specifically, do not refuse the whole archive, surface it to the owner as "this backup contains state this version of Lumina doesn't know how to restore."
- **Missing required member** → refuse the whole archive; a REQUIRED_AGENT_STATE member absent from an otherwise-valid manifest indicates a truncated or tampered archive, not a partial-restore opportunity.

---

## I. Rebinding Model

What a future Restore must validate or regenerate, concretely, based on what this pass found:

1. **`prefs.json["last_persona"]`** — an absolute path baked in at save time ([ui/main_window.py:1146-1148](ui/main_window.py:1146)). Restore must rewrite it to the destination install's `personas/` directory (or clear it) rather than restoring the string verbatim — the current code's own failure mode (`os.path.exists()` fails silently, falls back to no persona) is exactly the kind of "silently produce a persona preference pointing at a missing asset" outcome Section 9 says must never happen.
2. **`skills` table `path` column** — absolute, `BASE_DIR`-anchored ([core/skills.py:35-37](core/skills.py:35)). No code today rewrites this on install-location change. A2/A3 must add a rebind step: for each restored `skills` row, recompute `path` as `<dest_base_dir>/skills/<basename>`.
3. **`DATA_DIR/projects/<name>/binding.json`** — already correctly isolated as machine-local ([core/project_context.py:190-195](core/project_context.py:190)) by prior design work; Restore's job is only to *not* auto-trust the restored root path. Validate it exists; if not, prompt the owner to re-pick, exactly as Section 11 specifies.
4. **`coding_checkpoints` / `coding_validation_evidence`** — target-key/state-ref/scope-key fields are project-scoped and likely reference filesystem or git state that may not survive a project rebind. Standing principle applied here directly: retain as history, never treat as current authority until revalidated against the (possibly rebound) project.
5. **Flight Recorder's `runtime_id`/`process_id`/`worktree_id` fields** — these are labels on already-past events, not live pointers to anything that needs to resolve post-restore. No rebind action is needed; they simply shouldn't be misread as *current*-process identity after a restore.

Nothing in this pass found a rebind requirement for personas/avatars/voices/skills content itself (Section D) — those are already portable, relative-path text.

---

## J. Provenance Model

Values: `shipped_baseline_unmodified`, `shipped_baseline_modified`, `user_created`, `lumina_created`, `imported`, `unknown`, `retained_artifact`, `operator_authored`.

Mechanism: the shipped-baseline-hash diff described in Section D is primary. A DB `created_at` timestamp is a secondary, weaker signal (tells you *when* a row was inserted, not *who/what* created it). Git-tracked status is a release-repo-only forensic signal, not available to the running app at all, and was only usable in this pass because the investigation had access to `~/lumina-release`'s git history — a live install has no git and cannot lean on this.

**The retained-artifact case, concretely:** `skills/rubber-duck-debugging.md` was created by a real tool call (`save_skill`) during an actual eval run on 2026-07-24 ([eval/results/20260724_012017_raw.jsonl](eval/results/20260724_012017_raw.jsonl)), then deliberately excluded from git via a dedicated `.gitignore` line ([.gitignore:39](.gitignore:39)) rather than deleted — i.e. a human decision was already made to keep it as project folklore while keeping it out of the tracked release tree. Its provenance is unusual (test-harness origin) but its current retention is clearly intentional. Missing provenance information must never be read as a reason to exclude something from Agent Backup; the design principle here is: **strange origin does not equal unwanted current state.** The taxonomy captures this by tagging it `retained_artifact` rather than inventing a rule that silently prunes anything whose origin looks like test/eval contamination.

**The operator-authored case, concretely:** `identity_trailers.json` (Section C) is human/operator-created engineering metadata — not shipped product content, not something the running Agent ever writes or reads, and not a stand-in for any of the other six values (it isn't `unknown` — its origin is perfectly well understood, just outside the Agent-recovery frame entirely). `operator_authored` exists precisely for this shape of item: content worth keeping in a full disaster-recovery archive, tagged so a future Restore's preview (Section 22) can describe it accurately to the owner rather than lumping it in with `unknown` or forcing it into `user_created`, which would misleadingly imply it's part of what the Agent herself considers her own state.

---

## K. Exclusions

```
credentials.json          — EXCLUDED_BY_DESIGN. Security boundary. Restore must never overwrite,
                             delete, blank, migrate, or otherwise touch the destination credentials
                             store, in either direction. Already correctly excluded by the existing
                             memory_backup.py (it lives outside DATA_DIR entirely, so there was never
                             even an accidental path for it to leak in).
worktrees/ (DATA_DIR)      — EXCLUDED. Ephemeral coding-agent git working copies.
sandbox_tmp/,
browser_screenshots/,
eval/ harness fixtures     — EXCLUDED. Scratch/dev-tooling, no cross-session meaning.
```

`identity_trailers.json` is **not** in this exclusions list — it is included as optional, non-required operator metadata (Section C, Section L). It has zero current code consumers so it carries no runtime authority and never gates Agent recovery, but it is deliberately kept, not treated as dead weight or as a secret to guard.

No venvs, installed packages, model caches, or browser caches exist under either `DATA_DIR` or `BASE_DIR` in this product's actual layout — those categories from the generic exclusion list in the campaign brief don't currently apply to Lumina's real filesystem footprint and are noted here only to confirm they were checked, not silently assumed.

---

## L. Known Product Limitations

- **Scheduled/background tasks are not durable.** `core/task_queue.py`'s scheduler is a plain in-process `heapq` + dict ([core/task_queue.py:25-27](core/task_queue.py:25)), run by a daemon thread whose own docstring says it "dies with the process, no explicit shutdown path needed" ([core/task_queue.py:223](core/task_queue.py:223)). There is nothing to back up here — this is a real product gap, not an Agent Backup design gap, and this campaign does not expand scope to fix it. If future scheduler durability is wanted, it is a separate, later campaign.
- **`ledger.db` is not WAL-checkpointed by the existing backup** — `core/memory_backup.py`'s explicit checkpoint calls cover only the main DB and Flight Recorder ([core/memory_backup.py:49-56](core/memory_backup.py:49)); `core/idempotency.py`'s `LEDGER_PATH` ([core/idempotency.py:13](core/idempotency.py:13)) was previously swept in only by the generic `os.walk`. **Decided (no longer an open question):** since Section C now classifies `ledger.db` as REBUILDABLE_GENERATED with a firm "always regenerate empty, never restore stale rows" policy, this WAL-checkpoint gap is moot by design — A2 does not need to fix it, because A2 should never be reading `ledger.db`'s content into the archive as meaningful state in the first place.
- **`skills.path` is baked in as an absolute, install-location-anchored path** ([core/skills.py:35-37](core/skills.py:35)), with no existing rebind/rewrite tooling. Section I flags the specific fix A2/A3 must add; today, restoring `lumina.db` onto a different `BASE_DIR` silently breaks every skill lookup even though the `.md` files themselves would restore correctly.
- **Flight Recorder has no internal schema-version marker.** The `events` table ([core/flight_recorder.py:84-102](core/flight_recorder.py:84)) has no `schema_version` column (unlike `coding_checkpoints`/`coding_validation_evidence`, which do), and retention policy (`FULL_RETENTION_SECONDS`, `ERROR_RETENTION_SECONDS`, `DEFAULT_MAX_DB_BYTES`) is a hardcoded Python constant, not persisted anywhere the DB file itself can report ([core/flight_recorder.py:60-66](core/flight_recorder.py:60)). The Agent Backup manifest is the only place this information can live externally — captured in each member's `database_metadata` block (Section E/G).
- **`identity_trailers.json` has zero runtime code consumers — decided, not left open.** Present at `~/.config/lumina/identity_trailers.json`; `grep -rn "identity_trailers"` across the repo returns nothing. This is not a product gap to fix — it's genuinely not something the running app depends on. Decision: keep it, include it as optional operator metadata in full disaster-recovery backups, tag it `non_runtime_authority` so no future Restore logic mistakes it for something Agent recovery depends on, and never fold it into the credentials boundary (it holds no secrets and was never permissioned like one — `0600` vs. `identity_trailers.json`'s ordinary group/world-readable mode already reflects that distinction on disk today).
- **`projects/*/chats.json`'s exact location was not conclusively confirmed** by either source-vet pass (referenced by `.gitignore:7` and `tools/projects.py`'s comments, but neither pass pinned its concrete runtime directory). This is a genuine open item for A2 to close with one more targeted look before finalizing the Projects contract — it does not block A1's overall design, since worst case it slots into the existing `overlay/projects/<name>/` bucket once located.
- **No rebuild/reindex tool exists for the `skills`/`skills_fts` tables from `.md` files alone.** In principle this metadata is index-like and could be REBUILDABLE_GENERATED; in practice, with no such tool built, it must be treated as REQUIRED today. Building that tool is a reasonable candidate for a future slice, out of scope here.
- **Tool profiles have no confirmed runtime creation path** — only persona-level selection of an existing named profile was found. Flagged for product-owner confirmation; does not block archiving them via the same baseline-hash mechanism as everything else in Section D.

---

## Forward Notice: Restore's Trust Boundary (not designed here)

A1 does not design Restore, but the manifest/archive format must be shaped so a future hostile-archive validation kernel (A4) can actually enforce these distinctions later. Recorded here as a requirement on the format, not an implementation:

- Manifest-declared paths, ZIP member names, and hash fields are **untrusted input** at restore time — Restore must independently derive/validate archive paths against the fixed layout in Section F rather than trusting whatever a manifest claims, and must verify every hash rather than trusting a `sha256` field at face value.
- A manifest claiming a Python custom tool or a pending action is "approved" must **never** be treated as currently approved on the destination machine purely because the archive says so — approval state is exactly the kind of thing a hostile or stale archive could lie about, and Section 16/K's credentials boundary is the sharpest instance of a broader rule: restored *authority* claims need independent revalidation, restored *content* does not.
- This document's job was to make sure the format has the semantic distinctions (state class, required-vs-optional, rebind-required, provenance) that a later trust kernel needs to reason about — not to build that kernel.

---

## M. Recommended A2 Implementation Contract

Backup (A2) must:

1. Collect every REQUIRED_AGENT_STATE, USER_OVERLAY, and REBIND_REQUIRED member enumerated in Section C, laid out per Section F.
2. Extend the existing quiescence sequence: WAL-checkpoint `lumina.db` (existing) and `flight_recorder.db` (existing). `ledger.db` is **not** part of this sequence — the decision is made, not deferred: A2 must never checkpoint, read, or archive its content; it only records the `regenerate` entry from Section E/F in the manifest.
3. Compute `sha256` + `size` for every archived member; write `manifest.json` per the Section E schema.
4. Generate/consult a `metadata/shipped_baseline_hashes.json` reference set (Section D) to populate each overlay member's `provenance` field — this is the one genuinely new piece of infrastructure this design requires beyond "zip more directories."
5. Record required-vs-optional and portability exactly as tabulated in Section C; write the `exclusions` block (Section K) explicitly, including the `credentials = excluded_by_design` record, without ever touching or even listing the file's contents.
6. Produce a Backup Receipt (Section 21's shape: true item counts per category, not fabricated) so a later UI can render a truthful pre-restore preview (Section 22) from the manifest alone.
7. Explicitly must **not** implement: Restore itself, any UI, hash verification *on restore* (that's Restore's job), migration logic, credential export/encryption (a separate future A8 campaign with its own threat model), or the `skills.path`/`last_persona` rebind rewrites (those are Restore-time operations, Section I) — A2 only needs to *record* enough manifest metadata for a later Restore to perform them correctly.

---

## Entry Criteria for A2

**A2 may begin.** This document establishes, from live source rather than assumption:

- a canonical 5-class state taxonomy (Section B) with a cross-cutting provenance model (Section J) that correctly handles the mixed-ownership and unusual-provenance cases the campaign brief specifically flagged (rubber duck, personas, Skills);
- a complete state ownership matrix (Section C) covering every durable-state category the brief asked about, sourced to file:line, including several findings not previously documented anywhere on disk (the `skills.path` absolute-path rebind gap, the `last_persona` absolute-path gap, the `ledger.db` checkpoint gap, the orphaned `identity_trailers.json`);
- concrete overlay semantics (Section D) that solve "shipped vs. user-created" generically via a baseline-hash mechanism, rather than per-directory special-casing;
- a versioned `manifest.json` v1 schema with a real example (Section E), a logical archive layout with no absolute-path or `../` leakage (Section F), per-member metadata requirements (Section G), a compatibility/versioning model (Section H), a rebinding model naming every concrete rebind the current codebase actually requires (Section I), explicit deliberate exclusions (Section K) and honestly-labeled known product limitations (Section L) rather than papering over gaps.

Of the three items originally flagged as open, two are now resolved by the amendment below (`ledger.db`'s classification is decided; `identity_trailers.json`'s fate is decided). One remains, **non-blocking**, homework for early A2:

1. Confirm the actual on-disk location of `projects/*/chats.json`.

This does not block starting A2's implementation of Backup collection against the contract established here.

---

**STOP.** No code changes were made. No source-tree migration occurred. No backup was executed. No Restore was implemented. No commit, push, or release→dev mirror occurred. No opportunistic cleanup was performed. `skills/rubber-duck-debugging.md` was read for evidence and is otherwise untouched.
