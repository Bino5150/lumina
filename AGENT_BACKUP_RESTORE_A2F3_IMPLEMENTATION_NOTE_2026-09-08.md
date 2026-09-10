# AGENT-BACKUP-RESTORE-A2F3 — Implementation Note

**Status:** Repaired candidate ready for AGENT-BACKUP-RESTORE-A2R4. **Not committed, not pushed, not mirrored.** Per the mission's STOP-BEFORE-CHECKPOINT instruction, this only records what changed, why, and what remains.

## Repository gate (Section 0)

- Release (`~/lumina-release`, branch `main`): HEAD `ee40259` == `origin/main`, working tree clean apart from this campaign's own untracked candidate files (`AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md`, `AGENT_BACKUP_RESTORE_A2F_IMPLEMENTATION_NOTE_2026-09-08.md`, `AGENT_BACKUP_RESTORE_A2F2_IMPLEMENTATION_NOTE_2026-09-08.md`, this file, `core/agent_backup.py`, `tests/test_agent_backup.py`, `metadata/`). No merge/rebase/cherry-pick/bisect state present.
- Dev (`~/lumina`, branch `dev-local`): HEAD `9b09542`, one modified tracked file (`projects/lumina-dev/codebase.md`) and unrelated untracked skill files — pre-existing residue from other work, not touched by this session. `release-local` remote push is structurally `DISABLED`. Release `ee40259` confirmed an ancestor of dev's `9b09542` (merged in as `9b09542 Merge release: README and persona assets`). Dev was not touched.

## What A2R3 found still broken, and the repair for each

### 1. SQLite connection identity could still race to the credentials inode (BLOCKER)

A2R2's defense (`_verify_sqlite_source_identity` called again immediately after `sqlite3.connect()`) re-derived identity from a **pathname** re-`stat()`. A2R3 defeated it: swap the pathname to a hardlink of a valid-SQLite fake credentials store during `connect()`'s own internal open, let the connection open the credentials inode, restore the pathname to the safe file *before* the post-connect re-check runs. The re-check `stat()`s the now-restored, safe pathname and is satisfied — but the live connection still holds the credentials inode open. Any check that re-derives identity from a **path** is structurally blind to this, no matter how tightly it's timed, because the path and the fd it was used to open are two different things the moment a rename/relink happens in between.

**Repair:** `_connect_sqlite_with_bound_identity()` ([core/agent_backup.py:614](core/agent_backup.py:614)) never stats a path after connecting. It calls the connect function, then enumerates this process's own currently-open file descriptors via `/proc/self/fd`, and `os.stat()`s each one directly — `/proc/self/fd/N` is a magic symlink; stating it (following the symlink) returns the metadata of the file description that fd is *currently* bound to, fixed permanently the instant the underlying `open()` syscall succeeded, immune to any later rename of the path used to open it. If any open descriptor is bound to `credentials_identity`, the connection is rejected outright (REJECTED, no archive). If none is bound to `expected_identity` (the identity that passed the pre-connect trust-boundary check), the connection is likewise rejected — fail closed, never assumed safe. `/proc/self/fd` unavailable on the platform is itself a fail-closed `AgentBackupError`, never a silent fallback to the weaker path-based check.

Two design decisions worth recording:
- **Static full listing, not before/after diffing.** A before/after diff was tried first and rejected after direct measurement (`Bash`, not assumed) showed `/proc/self/fd` listings can themselves transiently include the scan's own internal directory descriptor, producing a "new" fd entry that's already closed by the time it's inspected — the diff missed the actual persistent connection fd entirely in that measurement. A single static listing taken immediately after `open_fn()` returns measured reliably instead; membership-checking (not "newness") is safe here because this module's staging pipeline is single-purpose and synchronous.
- **`sqlite3.connect()` opens the file descriptor synchronously, even in read-only URI mode, even before any query** — confirmed empirically via `/proc/self/fd` inspection immediately after `connect()` with no query issued yet, not assumed from documentation.

`_snapshot_sqlite()` now calls `_connect_sqlite_with_bound_identity` for the source connection when boundary-checking is enabled (`canonical_root_real is not None`, the production path); the old post-connect `_verify_sqlite_source_identity()` re-check is removed (superseded, not left dead alongside the new one). The pre-connect `_verify_sqlite_source_identity()` call in `_collect_database_members` is unchanged — it's what establishes `expected_identity` in the first place.

**Tests:** `test_sqlite_connect_time_hardlink_swap_and_restore_still_caught`, `test_sqlite_connect_time_hardlink_swap_flight_recorder_still_caught` (both DB roles, per the mission's ask), `test_connect_sqlite_with_bound_identity_rejects_credentials_fd_directly`, `test_connect_sqlite_with_bound_identity_fails_closed_when_proc_fd_missing`. Self-adversarial: blocked live, outside pytest (Section "Self-adversarial pass" below) — confirmed the sabotage actually fired (`swapped["done"]`) before confirming no leak, per the discipline of proving the attack actually executed rather than trivially passing because it never ran.

### 2. Full-backup verifier did not enforce complete canonical inventory (BLOCKER)

A2R3 constructed a hand-built `backup_mode: "full"` manifest containing only `lumina.db` (prefs and Flight Recorder removed) and `verify_agent_backup()` returned `valid: true`. Completeness was only ever enforced by the *build* path (`build_agent_backup`'s own `archive_paths_present` floor check) — never independently by the verifier, which is what anything restoring from an untrusted archive actually has to trust.

**Repair:** `_validate_full_backup_required_floor()` ([core/agent_backup.py:2127](core/agent_backup.py:2127)) is now called from `_validate_manifest_schema` and requires all three of `_REQUIRED_FLOOR_LOGICAL_IDS` (`state.databases.lumina_db`, `state.telemetry.flight_recorder_db`, `state.preferences.prefs_json`) to be present by `logical_id`, independent of the build path. `tool_audit_log` is deliberately excluded from this floor — its source only exists once a custom tool has ever been approved, a genuine valid absence per A1, unlike the three unconditionally-required members.

**Tests:** `test_verify_rejects_full_backup_missing_a_required_floor_member` (parametrized: drop prefs, drop Flight Recorder, one at a time — proves the floor check names the specific missing member), `test_verify_rejects_full_backup_with_only_lumina_db` (the literal A2R3 reproduction).

### 3. Required payloads were structurally checked but not semantically identified (BLOCKER)

A2R3 proved: `prefs.json = "not-json"` with a correct hash/size still verified `valid: true`; an unrelated, structurally-healthy SQLite database (valid `quick_check`, containing only an unrelated table) placed at `state/databases/lumina.db` with a correct hash/size also still verified `valid: true`. Hash/size correctness proves "these are the bytes that were archived," not "this is genuinely Lumina's own state."

**Repair — preferences:** `_verify_prefs_payload()` ([core/agent_backup.py:2257](core/agent_backup.py:2257)) requires valid UTF-8, valid JSON, and a top-level JSON object. Deliberately requires **no specific key** — `core/persistence.py`'s own `load()` merges any subset of keys onto `_defaults` and tolerates any of them being absent, so requiring a specific key would reject a genuinely valid `prefs.json` a real machine simply hasn't written yet.

**Repair — primary database:** `_LUMINA_DB_REQUIRED_TABLES`/`_verify_role_schema()` require the table/column set `core/agent.py`'s `Agent.__init__` initializes **unconditionally** on every startup regardless of channel/owner (`init_memory_db()`, `init_chat_db()`, `init_skills_db()` at [core/agent.py:1437-1484](core/agent.py:1437), traced directly, not inferred) — `memories`, `chats`, `chat_messages`, `skills`, with their real column sets sourced from `tools/memory.py` and `core/skills.py`. Deliberately **not** the full table list (palace/knowledge/coding-checkpoint tables are created lazily on first tool use — requiring them would reject a genuinely valid, merely lightly-used, real `lumina.db`).

**Repair — Flight Recorder:** `_FLIGHT_RECORDER_DB_REQUIRED_TABLES` requires the `events` table with its exact column set, sourced directly from `core/flight_recorder.py`'s own `_SCHEMA_SQL`.

**Verifier wiring:** `_verify_sqlite_payload()` gained an optional `role` parameter; `_verify_agent_backup_inner` derives the role from the member's `logical_id` via `_SQLITE_ROLE_BY_LOGICAL_ID` and applies it independently of `database_metadata`/`content_kind`/`logical_id` claims — the check runs against the actual archived bytes, extracted to a throwaway temp file, never against manifest-recorded metadata. `database_snapshots_verified` in the receipt already only counted `verified_sqlite_members`, which now only grows on a role-valid payload, so this closed automatically, no separate repair needed there.

**Tests:** `test_verify_rejects_unrelated_healthy_sqlite_as_lumina_db`, `test_verify_rejects_unrelated_healthy_sqlite_as_flight_recorder_db`, `test_verify_rejects_prefs_syntactically_invalid_json`, `test_verify_rejects_prefs_wrong_top_level_json_type`.

### 4. Manifest-authored semantics could still redefine Lumina's v1 recovery policy (BLOCKER)

A2R3 showed hostile manifests could still redefine: `lumina_db.portable=EXCLUDED`, `lumina_db.restore_policy=delete_without_restore`, prefs at the wrong canonical archive path, `backup_mode=partial`, `hash_algorithm=md5`, and executable-authority claims via `restore_and_autoapprove`/`approved=true` on custom tools and `tool_audit.log`.

**Repair — canonical member policy widened.** `_CANONICAL_MEMBER_POLICY` ([core/agent_backup.py:136](core/agent_backup.py:136)) previously fixed only `required`/`state_class`/`content_kind` for the three canonical members. It now also fixes `archive_path`, `portable`, `restore_policy`, and (where applicable) `rebind_policy` for all three, **plus a fourth entry, `state.audit.tool_audit_log`**, closing Section 6's explicit ask ("enforce the canonical fixed policy for this member too"). Values are sourced from `AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md` Section C's narrative decision table — cross-checked against, not blindly copied from, A1's own illustrative JSON example, which disagrees with its own Section C table in one spot (`lumina_db`'s `portable` value: the example says `PORTABLE`, but Section C's table and Section D's own `PORTABLE_WITH_REBIND` definition both say the `skills.path` rebind need makes it `PORTABLE_WITH_REBIND` — the narrative table is the decision record, the JSON snippet is illustrative and inconsistent with its own document in this one field).

**Repair — root contract.** `_validate_root_contract()` now requires `backup_mode == "full"` and `hash_algorithm == "sha256"` exactly — v1 supports no other mode or algorithm, regardless of what an archive's own manifest claims.

**Repair — dynamic custom-tool + tool-audit-log authority.** `_validate_authority_bearing_member_schema()` (Section 6, see finding 6 below) closes the `restore_and_autoapprove`/`approved=true` gap generically, by field-allowlisting rather than keyword-blacklisting.

**Tests:** parametrized `test_verify_rejects_root_contract_substitution` (backup_mode, hash_algorithm), `test_verify_rejects_lumina_db_canonical_field_substitution` (portable, restore_policy, archive_path), `test_verify_rejects_prefs_wrong_canonical_archive_path`, `test_verify_rejects_flight_recorder_required_false`.

### 5. Dynamic custom-tool authority semantics (BLOCKER)

A2R3's own framing: don't reject arbitrary unknown fields just because their names sound suspicious — define the v1 schema tightly, or explicitly whitelist permitted optional fields.

**Repair:** `_AUTHORITY_BEARING_ALLOWED_FIELDS` ([core/agent_backup.py:201](core/agent_backup.py:201)) is a tight, explicit allowlist (`logical_id`, `state_class`, `content_kind`, `archive_path`, `sha256`, `size`, `required`, `portable`, `restore_policy`, `rebind_policy`) — exactly the fields `_collect_custom_tool_members`/`_collect_audit_members` actually produce. `_validate_authority_bearing_member_schema()` applies it to every member whose `logical_id` starts with `state.custom_tools.` or equals `state.audit.tool_audit_log`, rejecting **any** extra field regardless of its name (`approved`, `autoapprove`, `destination_authority`, or anything else not already recognized) — this closes the class of attack, not just the two examples A2R3 demonstrated. The same function also enforces `_CUSTOM_TOOL_FIXED_POLICY`'s fixed values for dynamic (per-file) custom-tool members, since their `logical_id` is per-file and can't be covered by `_CANONICAL_MEMBER_POLICY`'s fixed-key dict the way `tool_audit_log` now is.

**Tests:** `test_verify_rejects_custom_tool_approved_true_field`, `test_verify_rejects_tool_audit_autoapprove_semantics`, `test_verify_rejects_tool_audit_destination_authority_field` (an unrecognized field, not a recognized field with a suspicious value — proves the allowlist itself, not just keyword-matching). `test_verify_rejects_custom_tool_with_autoapprove_semantics` (pre-existing, A2F2-era) still passes against the rewritten check.

### 6. Ledger regenerate semantics — value-checked, not just presence-checked (BLOCKER)

Folded together with finding 7 below since both live in `_validate_canonical_policy_records`. A2R2/A2F2 already enforced *presence* of a canonical ledger regenerate entry; A2R3 found `state_class=EXCLUDED` and `restore_policy=restore_stale_rows` variants still passed, since presence-by-`logical_id` alone was checked, not the entry's own field values.

**Repair:** `_validate_canonical_policy_records()` now requires the single ledger regenerate entry to carry exactly `state_class=='REBUILDABLE_GENERATED'` and `restore_policy=='always_regenerate_empty'`, not merely the right `logical_id`. Physical ledger payload rejection (Section 8's allowlist-based check) is unchanged, already correct.

**Tests:** parametrized `test_verify_rejects_ledger_regenerate_field_substitution` (`state_class`, `restore_policy`).

### 7. State-bearing overlay/custom-tool special files could disappear silently (BLOCKER)

A2R3 replaced the sole `personas/lumina.json` with a FIFO and with a Unix socket; the old `_collect_overlay_dir` treated "matches the filename filter but isn't a regular file" (after excluding symlinks, which are handled separately) as a quiet `continue` — "a shape oddity, not a required-content disappearance." A full backup was published claiming complete disaster recovery with zero personas captured. The same class of gap existed in `_collect_custom_tool_members` (a `.py`-named FIFO went through `_stage_plain_file`'s existing "not a regular file → warn and exclude" path, which is *correct* for symlinks but was never meant to also cover this) and in the ad-hoc `os.path.isfile(x) and not os.path.islink(x)` checks used for Project documents (`project.md`, `binding.json`, `chats.json`, `projectlist.md`).

**Repair:** `_reject_if_special_file()` ([core/agent_backup.py:508](core/agent_backup.py:508)) raises `AgentBackupError` (SECURITY_REJECTED_STATE — the state exists but cannot be faithfully captured, distinct from ordinary absence) for a path that `lstat`s successfully as neither a symlink nor a regular file. Wired into: `_collect_overlay_dir`'s per-file loop (inline, since it also needed to keep escalating for a directory shape-collision, not just FIFO/socket/device); `_collect_custom_tool_members`'s walk loop (a pre-check before `_stage_plain_file`); and each of the four Project-document `else` branches in `_collect_project_members` (`projectlist.md`, `project.md`, `binding.json`, `chats.json` — deliberately **not** `codebase.md`, which is `REBUILDABLE_GENERATED`/`required: false` and disposable by design).

A symlink at the same position is deliberately **not** escalated — that's the pre-existing, unchanged "a hostile or accidental symlink must never be able to permanently deny a backup" policy, since the original content is plausibly still safe elsewhere. A FIFO/socket/device has no such fallback: the real bytes that used to be there are simply gone, and nothing legitimate ever creates a socket named `lumina.json`.

**Tests:** `test_sole_persona_replaced_with_fifo_fails_backup`, `test_sole_persona_replaced_with_unix_socket_fails_backup`, `test_skill_markdown_replaced_with_fifo_fails_backup`, `test_custom_tool_replaced_with_special_file_fails_backup`, `test_custom_tool_replaced_with_symlink_still_excluded_not_leaked` (the symlink contrast case, proving the two paths are genuinely different, not the same code accidentally doing the same thing either way).

## Remaining HIGH issues, repaired

### 8. Baseline artifact substitution/staleness was trusted (HIGH)

A2R3 forged a `metadata/shipped_baseline_hashes.json` claiming a nonexistent `git_commit` and attacker-selected hashes; it was trusted outright (the old `_load_baseline_hashes` only checked format/version/`lumina_version`, never cross-checked the claimed commit or hashes against anything), causing a live persona to be falsely classified `shipped_baseline_unmodified`.

**Repair:** `_verify_baseline_against_git()` ([core/agent_backup.py:895](core/agent_backup.py:895)) is called from `_load_baseline_hashes` (now given `base_dir`) after the existing structural checks pass. It requires: `base_dir` is a git checkout; the claimed `git_commit` resolves to a real commit in it (`git cat-file -e <commit>^{commit}`); every path in `files{}` falls inside `_BASELINE_CATEGORIES`'s allowed shipped-overlay namespace (the same allowlist `generate_baseline_hashes` itself is scoped to); and every claimed hash matches the **actual** git blob bytes at that commit (`git show <commit>:<path>`, independently re-hashed — never trusting the artifact's own self-declared hash map). Any failure — nonexistent commit, a real-but-different commit with stale hashes, an out-of-namespace path, a tampered hash, no git checkout at all — falls back to the same truthful `{} + warning` path every other baseline rejection already used; nothing here invents trust where A1's "no-git installations: do not invent trust" applies.

Also closed in the same pass: the baseline's own `hash_algorithm` field was never checked at all (not merely under-checked) — `_load_baseline_hashes` now requires it to equal `"sha256"`.

**Tests:** `test_baseline_nonexistent_git_commit_falls_back_to_unknown`, `test_baseline_different_real_commit_falls_back_to_unknown` (a genuinely resolvable commit, but a stale/mismatched hash claim — the more realistic forgery than a wholly nonexistent commit), `test_baseline_hash_algorithm_not_sha256_falls_back_to_unknown`, `test_baseline_tampered_file_hash_falls_back_to_unknown`, `test_baseline_malformed_files_map_falls_back_to_unknown`, `test_baseline_unknown_overlay_path_falls_back_to_unknown`, `test_baseline_missing_git_metadata_context_falls_back_to_unknown` (no-git-checkout case), `test_baseline_valid_artifact_matching_current_release_is_trusted` (the positive case, run against the **real** release checkout, not a synthetic one). Three pre-existing A2F2 tests (`test_duck_present_but_not_in_baseline_classifies_unknown_not_shipped`, `test_modified_shipped_overlay_classified_as_modified`, `test_default_baseline_path_auto_discovered_from_base_dir`) needed their fixtures rebuilt around a real git-backed baseline (`_real_baseline_for()`, a new helper) rather than a hand-rolled fake-`git_commit` one, since the new verification correctly now rejects the latter — this is the new check doing its job on the test fixtures themselves, not a weakening of the tests.

### 9. Portable namespace did not reject Windows trailing-dot/space ambiguity (HIGH)

**Repair:** `_check_path_and_identity_rules()` now rejects any archive-path segment ending in a trailing `.` or space (stripped silently by Windows filesystem APIs — two archive paths differing only in this suffix would collide or lose the suffix unpredictably on restore to a Windows destination), and any segment whose stem (before the first `.`) case-insensitively matches a reserved Windows device name (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`). The archive namespace already targets cross-platform round-tripping (members are tagged `PORTABLE`/`PORTABLE_WITH_REBIND` throughout, not Linux-only), so both are rejected outright rather than silently accepted and left to fail unpredictably at restore time.

**Tests:** parametrized `test_verify_rejects_windows_trailing_dot_or_space` (`duck.md.`, `quack.md ` with trailing space, `demo./project.md`, `demo /project.md`), parametrized `test_verify_rejects_windows_reserved_device_names` (`CON`, `con` — case-insensitivity, `PRN`, `COM1`, `LPT1`, `NUL`). Previously-proven NFC/casefold/reserved-manifest/`..`/backslash checks are unchanged and still pass (existing tests, unmodified).

## Remaining MEDIUM issues, repaired

### 10. Receipt arrays/policy claims could still be manipulated (MEDIUM)

A2R3 appended a scalar to `members[]`; verification silently ignored it (the old code filtered non-dict entries via a bare `isinstance` list comprehension with no corresponding error) while the receipt's `member_count`, built from the raw manifest array, still counted it. Separately, contradictory/duplicate credentials-exclusion records could coexist with a valid canonical one.

**Repair — malformed array entries invalidate.** `_validate_manifest_schema` now explicitly flags any non-object entry in `members[]`, `exclusions[]`, or any non-string entry in `warnings[]` as an error (previously only `regenerate[]` did this). A malformed entry anywhere now makes the whole archive `valid: false`, so `build_backup_receipt` — which already checked `report["valid"]` first — reports `FAIL` rather than a fabricated `PASS` with an inflated count.

**Repair — canonical records are unique.** `_validate_canonical_policy_records` now counts entries matching the canonical `logical_id` for both the credentials exclusion and the ledger regenerate entry; more than one is an explicit "duplicates/contradictions are never valid" error, not silently tolerated alongside a valid one.

**Repair — receipt derives from structured verifier evidence.** `_verify_agent_backup_inner` now returns `validated_member_count`/`validated_regenerate_count` (counts of well-formed dict entries, computed by the verifier itself); `build_backup_receipt` reads these instead of `len(manifest["members"])`/`len(manifest["regenerate"])` directly. On a passing report these are numerically equal to the raw counts (a malformed entry anywhere already fails verification, per the repair above) — the receipt reads the verifier's own evidence on principle, not because the numbers would otherwise differ.

**Tests:** parametrized `test_verify_rejects_scalar_array_entries` (`members`, `regenerate`, `exclusions`), `test_receipt_never_inflates_member_count_from_scalar_entry`, `test_verify_rejects_duplicate_ledger_regenerate`, `test_verify_rejects_duplicate_contradictory_credentials_exclusions`.

### 11. Full release suite must complete natively (MEDIUM — gate, not a code finding)

See "Full release suite" below.

## Preserved, not rewritten (Section 22)

A2R3's own PASS list re-confirmed unmodified in mechanism by the full focused suite still passing: SQLite online-backup snapshot consistency, WAL-aware concurrent-writer handling, bounded lock timeout, immutable staging + hash-staged-bytes-not-reopened-live-bytes, destination preflight/safety, verify-before-`os.replace()`, prior-good-backup survival on build/verify failure, Project split-root assembly (data-only project discovery), NFC/casefold Unicode collision detection, dirty-working-tree baseline generation resistance (the *generator* already hashed git objects, not working-tree bytes — this campaign added *runtime* authentication of the resulting artifact, a different layer), custom-tool normal quarantine output, test isolation (no test touches real machine state).

## Self-adversarial pass (Section 23)

Fourteen attacks independently re-run; the five highest-risk/hardest-to-unit-test ones were run **live, outside pytest** (script: scratchpad `adversarial_pass_a2f3.py`, run from `~/lumina-release` via a real Python process, not the test harness), the rest confirmed via the focused suite's dedicated regression tests (each named above under its finding).

| Attack | Result | Evidence |
|---|---|---|
| SQLite connect-time credential swap (lumina.db) | **Blocked** | live script — sabotage confirmed fired, connection rejected, no archive published |
| SQLite connect-time credential swap (flight_recorder.db) | **Blocked** | focused test (`test_sqlite_connect_time_hardlink_swap_flight_recorder_still_caught`) |
| Full archive missing prefs.json | **Blocked** | focused test |
| Full archive missing Flight Recorder | **Blocked** | focused test |
| Unrelated healthy SQLite as lumina.db | **Blocked** | live script + focused test |
| Unrelated healthy SQLite as Flight Recorder | **Blocked** | focused test |
| Invalid prefs JSON (syntax + wrong top-level type) | **Blocked** | focused test |
| Custom tool `restore_and_autoapprove`/`approved=true` | **Blocked** | focused test |
| tool_audit.log autoapprove / `destination_authority` field | **Blocked** | focused test |
| Wrong canonical policies (portable/restore_policy/archive_path/backup_mode/hash_algorithm) | **Blocked** | focused test (parametrized) |
| Forged baseline (nonexistent git_commit) | **Blocked** | live script — provenance confirmed `unknown`, not fabricated |
| Trailing-dot/space namespace + reserved device names | **Blocked** | focused test (parametrized) |
| Special-file persona loss (FIFO, Unix socket) | **Blocked** | live script (socket) + focused test (FIFO, socket, skill FIFO, custom-tool FIFO) |
| Receipt scalar inflation | **Blocked** | live script — receipt confirmed `FAIL`, not a fabricated `PASS` |
| Contradictory credentials exclusion | **Blocked** | focused test |

14/14 blocked (counting the two SQLite-role variants of the first attack separately, matching A2F2's own convention).

## Focused tests (Section 24)

```
python3 -m pytest tests/test_agent_backup.py -q
```

```
146 passed, 1 warning in 18.01s
```

The one warning is the same expected `UserWarning: Duplicate name: 'manifest.json'` from `zipfile` itself, emitted by `test_verify_rejects_duplicate_zip_member_name` deliberately writing a duplicate entry to construct its fixture — not a defect, unchanged from A2F2.

`146 = 93 (A2F2 baseline) + 53` new regression tests across Sections 16-21 (some collected as multiple parametrized cases from fewer `def test_` bodies — 131 `def test_` functions in the file collect to 146 test items).

## Full release suite (Section 25)

A2R3's own run of this gate did not complete (stalled at ~73% in `tests/test_review_panel.py`, killed via SIGTERM, native exit 143 — no completed green receipt existed for the A2F2 candidate). This run was launched fresh, in the background, and allowed to run to completion with no intervention — not killed, not inferred:

```
python3 -m pytest -q
```

```
3708 passed, 1 warning in 472.30s (0:07:52)
[exited with code 0]
```

The run passed straight through `tests/test_review_panel.py` (32/32, no stall) and every file after it with no hang, no kill, no timeout. This confirms the discipline note already on file (`project_full_suite_intermittent_deadlock` — a rare, previously-confirmed-by-the-owner, pre-existing/recurring race in an unrelated racing-agents test, not something this campaign introduced or needs to fix): A2R3's stall was independent pre-existing suite nondeterminism, not caused by the Agent Backup changes in this candidate.

The same expected `UserWarning: Duplicate name: 'manifest.json'` as the focused run above, from the same deliberate fixture — not a defect.

One honestly-reported discrepancy, not smoothed over: a fresh `pytest --collect-only -q` run immediately afterward (on the exact same, unmodified tree) reports `3709 tests collected`, one more than the `3708 passed` the actual run reported — with zero failures, errors, or skips anywhere in that run's output. `tests/test_agent_backup.py` alone independently re-collects to exactly 146, matching the focused run above, so the one-test delta is not this campaign's own suite. It was not chased further — the gate this section exists to enforce (a completed native run, zero failures) is met either way, and re-deriving one collection-count mismatch in ~3.7k unrelated tests is outside this campaign's scope; flagged here rather than silently reconciled so a future session isn't surprised by it.

## STOP

After implementation and verification: no commit, no push, no mirror, no cleanup beyond what's described above. Returned as a candidate for **AGENT-BACKUP-RESTORE-A2R4**.
