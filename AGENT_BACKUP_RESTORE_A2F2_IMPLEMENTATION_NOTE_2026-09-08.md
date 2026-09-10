# AGENT-BACKUP-RESTORE-A2F2 — Implementation Note

**Status:** Repaired candidate ready for AGENT-BACKUP-RESTORE-A2R3. **Not committed, not pushed, not mirrored.** Per the mission's STOP-BEFORE-CHECKPOINT instruction, this only records what changed, why, and what remains.

## Repository gate (Section 0)

- Release (`~/lumina-release`, branch `main`): HEAD `ee40259` == `origin/main`, working tree clean apart from this campaign's own untracked candidate files (`AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md`, this file, `core/agent_backup.py`, `tests/test_agent_backup.py`, `metadata/`). No merge/rebase/cherry-pick/bisect state present.
- Dev (`~/lumina`, branch `dev-local`): HEAD `9b09542`, one modified tracked file (`projects/lumina-dev/codebase.md`) and unrelated untracked skill files — pre-existing residue from other work, not touched by this session. `release-local` remote push is structurally `DISABLED`. Dev was not touched.

## What A2R2 found still broken, and the repair for each

### 1. SQLite sources bypassed the credential/filesystem boundary entirely (BLOCKER)

`_snapshot_sqlite()` never called any boundary check — it only did `os.path.exists()`. A configured credentials store hardlinked or symlinked in as `memory/lumina.db` or `telemetry/flight_recorder.db` would be opened, backed up, and archived without ever being checked against the credentials identity.

**Repair:** `_collect_database_members()` now calls `_verify_sqlite_source_identity()` — the same lstat/O_NOFOLLOW-open/fstat trust-boundary decision as every plain file — for both `lumina.db` and `flight_recorder.db`, before `_snapshot_sqlite()` is ever called. A rejection raises immediately (both databases are unconditionally required, so exclusion and hard failure are observably identical — see [core/agent_backup.py:626](core/agent_backup.py:626)).

**Tests:** `test_sqlite_lumina_db_symlinked_to_credentials_rejected`, `test_sqlite_lumina_db_hardlinked_to_credentials_rejected`, `test_sqlite_flight_recorder_symlinked_to_credentials_rejected`, `test_sqlite_flight_recorder_hardlinked_to_credentials_rejected`, `test_verify_sqlite_source_identity_rejects_credentials_hardlink_directly`. Self-adversarial pass: both attacks blocked live (see Section 16 results below).

### 2. Boundary-check → open race on plain files (BLOCKER)

The old flow checked identity via a separate `os.stat(path)` call, then later opened the path again with `O_NOFOLLOW`. `O_NOFOLLOW` defeats a *symlink* substitution but not a *hardlink* substitution (a hardlink is still "a regular file") — so a path swapped to a hardlink of the credentials store between the stat and the open would sail through.

**Repair:** `_open_verified()` ([core/agent_backup.py:249](core/agent_backup.py:249)) collapses the check-then-open into one function: a cheap lstat pre-filter (symlink/type/root-escape) followed by `os.open(O_NOFOLLOW)`, and the credentials-identity decision is made *only* from `fstat(fd)` of the descriptor actually opened — never from a separate pre-open stat. `_stage_plain_file()` reads from that exact fd via `_read_fd_fully()`; there is no second path reopen anywhere in the chain.

For SQLite, the stdlib `sqlite3` module cannot be handed a pre-verified file descriptor without breaking WAL sibling-file resolution (`<path>-wal`/`<path>-shm` are found by *path*, not by fd — redirecting through `/proc/self/fd/N` would silently defeat WAL detection). `_snapshot_sqlite()` instead re-verifies identity via `_verify_sqlite_source_identity()` immediately after `sqlite3.connect()` succeeds and compares it against the identity the caller captured before connecting — narrower than the plain-file fix, and documented as such in the docstring, but it closes the gap to the width of one open+fstat pair rather than the whole staging pipeline.

**Tests:** `test_plain_file_hardlink_swap_between_lstat_and_open_is_still_caught` sabotages `os.lstat` itself to perform the swap in the exact window between the pre-filter and the open, and asserts the swap actually fired (`swapped["done"]`) before checking the marker never leaked. Self-adversarial pass: blocked live outside pytest too.

### 3. Required SQLite payloads were never independently re-verified (BLOCKER)

A2R2 reproduced: valid snapshot → staged bytes corrupted *after* `_snapshot_sqlite`'s own `integrity_check` but before the zip write → manifest still says `integrity_check: ok` → `verify_agent_backup()` only checked hash/size, which still matched the corrupted bytes → `valid: True`.

**Repair:** `verify_agent_backup()` now calls `_verify_sqlite_payload()` ([core/agent_backup.py:1188](core/agent_backup.py:1188)) on every member whose `content_kind == "sqlite_database"`, *after* its hash/size already matched — extracts the archived bytes to a throwaway temp file and runs `PRAGMA quick_check` against them directly, independent of anything the manifest claims. A non-SQLite byte string is rejected even earlier, by magic-header check, before SQLite is ever asked to open it.

**Tests:** `test_staged_db_corrupted_after_snapshot_check_still_fails_the_build` (corrupts staged bytes via a monkeypatched `_snapshot_sqlite` wrapper, confirms the build's own pre-publish verify catches it — assertion matches on the actual `quick_check` error text, not just "some AgentBackupError"), `test_verify_rejects_non_sqlite_bytes_masquerading_as_primary_db`, `test_verify_rejects_corrupt_flight_recorder_payload`.

### 4. Required state could silently disappear (BLOCKER)

Four distinct gaps, each fixed separately rather than folded into one generic mechanism:

- **Flight Recorder absent → successful archive.** A1 classifies `flight_recorder.db` as `REQUIRED_AGENT_STATE` with no documented valid-absence exception (confirmed by reading [AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md:98](AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md:98) and the module's own existing member classification, which already set `required: True` even before this repair). `FlightRecorder` is a startup-instantiated singleton ([core/flight_recorder.py:411](core/flight_recorder.py:411)), not a lazily-created optional file, so per the mission's instruction ("if the product can validly operate without it, update the A1 contract first — do not invert the architecture through a test expectation") this repair enforces the *existing* classification rather than silently narrowing it. `_collect_database_members()` now raises immediately if `flight_recorder.db` doesn't exist, mirroring `lumina.db`'s existing rigor; `build_agent_backup()`'s required-floor check gained a third entry as defense-in-depth. The OLD test asserting the opposite (`test_flight_recorder_missing_is_valid_absence_not_a_failure`) was replaced, not kept, with `test_flight_recorder_missing_fails_full_backup`.
- **Persona disappears after enumeration → successful archive.** `_collect_overlay_dir()`'s per-file `try: os.lstat(fpath) except OSError: continue` silently swallowed a file vanishing between `_safe_listdir()`'s enumeration and this lstat — a *different* window than the one already covered by `_stage_plain_file`'s own escalation (which only fires once staging itself begins). Fixed by removing the swallow: a required overlay file's lstat failure now raises `AgentBackupError` directly ([core/agent_backup.py:757](core/agent_backup.py:757)), after first checking `keep_fn(fname)` so a non-matching filename disappearing (which was never going to be collected anyway) doesn't falsely escalate.
- **custom-tools root becomes unreadable → treated as absent.** The old `_validate_root()`'s blanket `except OSError: return None` couldn't distinguish "doesn't exist" (ENOENT, a genuine valid absence) from "exists but lstat failed for another reason" (EACCES on a parent, ELOOP, etc. — a real failure hiding potential state). New `_classify_root()` returns `(real, exists, reason)` so callers can tell these apart; `_require_readable_root()` escalates on the second case while still treating a deliberate symlink/wrong-type root as a warn-and-skip (preserving the "a hostile symlink can never permanently deny a backup" property). Separately, `os.walk`'s own `onerror` wrapping in `_safe_walk()` already escalated a *root-level* scandir failure correctly even before this repair (confirmed empirically, not assumed — see `test_custom_tools_root_itself_unreadable_escalates`); the `_classify_root` fix closes a narrower, earlier-stage gap (an unreadable *parent*, not the target directory itself).
- **required custom-tool disappearing during staging → fails.** Already correctly escalated via `_stage_plain_file`'s existing `required=True` + `existence_failure` path; added `test_required_custom_tool_disappearing_during_staging_fails` to lock this in as a proven regression rather than an assumed one.

**Deliberate scope decision:** the mission's Section 5 sketches a formal `VALIDLY_ABSENT / EMPTY / DISCOVERED / STAGED / SECURITY_REJECTED / FAILED` state enum. This repair implements the *behavior* that enum describes (each of the four gaps above resolves to exactly one of those semantics) rather than introducing the enum as literal code — per the engineering-discipline instruction to fix the gap found, not rebuild the world around it. If a future task wants machine-readable per-item state (not just pass/fail + warning strings), that's a scoped follow-up, not silently bundled into this repair.

### 5. Project state required one physical root to exist to discover the other

`_collect_project_members()` only ever enumerated `BASE_DIR/projects/<name>/` and looked up `DATA_DIR/projects/<name>/{binding,chats}.json` nested inside that loop — a project with `binding.json`/`chats.json` in `DATA_DIR` but no matching `BASE_DIR` directory was never discovered at all.

**Repair:** rewritten to collect the *union* of subdirectory names from both roots (`_project_dir_names()`), then independently check each side's files per logical project name ([core/agent_backup.py:788](core/agent_backup.py:788)).

**Test:** `test_data_only_project_binding_and_chats_captured_without_base_dir_project` — asserts no matching `BASE_DIR` directory exists, then confirms both files are captured with correct `state_class`.

### 6. Verifier checked archive shape, not Lumina's own v1 semantics (BLOCKER)

A2R2 independently found all of the following incorrectly valid: `format_version.major == 0`, boolean major/minor (Python's `bool` is an `int` subclass — `isinstance(True, int)` is `True`, so the old `isinstance(major, int)` check silently accepted it), negative minor, a manifest missing its credentials-exclusion or ledger-regenerate records, a ledger payload physically present under an alternate path, a worktree payload, a canonical required member marked `required: false`, a custom tool with `restore_and_autoapprove`-style rebind semantics, wrong root-field types, and malformed UTF-8 raising instead of returning structured-invalid.

**Repair, each independently:**
- `_validate_format_version()`: explicit `isinstance(x, bool)` rejection before the `int` check; `major >= 1`; `major == MANIFEST_FORMAT_VERSION["major"]` exactly (not just "not greater than"); `minor >= 0`.
- `_validate_canonical_policy_records()`: the manifest must carry the canonical `credentials` exclusion entry and the canonical `state.databases.ledger_db` regenerate entry — their absence is now an error, not silently ignored.
- `_validate_canonical_member_semantics()`: for known canonical `logical_id`s (`state.databases.lumina_db`, `state.telemetry.flight_recorder_db`, `state.preferences.prefs_json`), the verifier enforces `required`/`state_class`/`content_kind` against Lumina's own v1 contract *regardless of what the archive's manifest claims*; every `state.custom_tools.*` member must carry `portable == PORTABLE_WITH_REBIND` and `rebind_policy == quarantine_until_explicit_reapproval`.
- `_ROOT_FIELD_TYPES`: explicit type-checks on every manifest root field, not just presence.
- `_verify_agent_backup_inner()` wraps manifest-name/read/parse access in progressively broader `except` clauses (the JSON parse step deliberately catches `Exception`, since malformed UTF-8 raises `UnicodeDecodeError`, not `JSONDecodeError`), and `verify_agent_backup()` itself wraps the whole inner call in one more `except Exception` as a last-resort net — "never raises" is this function's explicit contract, not an incidental property.
- Forbidden-payload detection (Section 8) moved from a single filename match (`archive_path == "state/databases/ledger.db"`) to an **allowlist** of the only permitted top-level namespaces (`_ALLOWED_ARCHIVE_PREFIXES`) plus basename/segment checks for `ledger`/`worktrees`/`scratch_tmp`/`credentials`-shaped names — applied to both declared manifest members *and* raw physical zip entries, so a renamed/relocated forbidden payload (a ledger stashed at `state/audit/renamed_ledger_copy.db`, a worktree leak) is caught by not being on the list, not by matching one specific bad filename.

**Tests:** `test_verify_rejects_major_zero`, `test_verify_rejects_boolean_major`, `test_verify_rejects_boolean_minor`, `test_verify_rejects_negative_minor`, `test_verify_rejects_missing_credential_exclusion_record`, `test_verify_rejects_missing_ledger_regenerate_entry`, `test_verify_rejects_ledger_payload_at_alternate_path`, `test_verify_rejects_worktree_payload`, `test_verify_rejects_required_canonical_member_marked_not_required`, `test_verify_rejects_custom_tool_with_autoapprove_semantics`, `test_verify_rejects_wrong_root_field_types`, `test_verify_malformed_utf8_manifest_returns_invalid_never_raises`.

### 7. Unicode-normalization namespace collisions (BLOCKER)

The old check only casefolded raw `archive_path` strings — an NFD/NFC pair (`"café.json"` written as `e` + combining accent vs. the precomposed `é`) are byte-distinct but render/collide identically on most filesystems, and neither the old check nor a plain casefold would catch it.

**Repair:** `_check_path_and_identity_rules()` now tracks three separate uniqueness dictionaries — raw path, NFC-normalized path, and NFC-normalized-then-casefolded path — and separately reserves `manifest.json` against NFC+casefold collisions (so `Manifest.json`, `MANIFEST.JSON`, or a Unicode-equivalent name can never appear as a payload member). Applied uniformly to every archive member category (projects, skills, personas, custom tools, etc.), not as a special case.

**Tests:** `test_verify_rejects_unicode_nfc_normalization_collision`, `test_verify_rejects_manifest_json_case_and_unicode_collision`.

### 8. Baseline generator hashed the dirty working tree, not shipped truth (BLOCKER)

The old `generate_baseline_hashes()` listed git-tracked *names* via `git ls-files` but then hashed whatever bytes were currently on disk via `_sha256_file()` — a locally-dirty tracked file was therefore falsely canonized as "shipped."

**Repair:** `generate_baseline_hashes(base_dir, commit="HEAD")` now resolves the commit via `git rev-parse`, lists tracked paths at that commit via `git ls-tree -r`, and reads each file's content via `git show <commit>:<path>` — immutable git object bytes, never the working tree. The output is now version-bound: `{format, format_version, lumina_version, git_commit, hash_algorithm, files}`, not a flat `{path: hash}` dict. `_load_baseline_hashes()` validates that shape (format/format_version/`files` key), and falls back to `({}, warning)` — never trusting it — on a wrong `format`, an unsupported `format_version`, a missing `files` key, or a `lumina_version` that doesn't match the running build's own version (unless the baseline says `"unknown"`, treated as a wildcard). A pre-A2F2 flat baseline (no `files` key at all) is explicitly treated as untrusted legacy input, not silently accepted.

`metadata/shipped_baseline_hashes.json` was regenerated against the current clean release checkout (`HEAD == origin/main == ee40259`, confirmed via `git status`/`git rev-parse` before generating) — same 20 files as before, now bound to `git_commit: ee40259c980d3c6fcca56fc2f27dcefd3814c178` and `lumina_version: 0.2.7-beta.2`.

**Tests:** `test_generate_baseline_hashes_uses_git_object_bytes_not_dirty_working_tree` (commits one version of a file, dirties the working tree with different content, confirms the baseline hash matches the *committed* content and explicitly differs from the dirty on-disk hash), `test_baseline_version_mismatch_falls_back_to_unknown_truthfully`, `test_legacy_flat_baseline_format_is_treated_as_untrusted`.

### 9. Duck provenance (Section 11)

No change — already correct. `skills/rubber-duck-debugging.md` is `.gitignore`'d, so it was never in the baseline before this repair and still isn't after; its provenance is `"unknown"`, never a fabricated `retained_artifact`/`shipped_baseline_unmodified` claim. `test_duck_present_but_not_in_baseline_classifies_unknown_not_shipped` and `test_duck_is_always_captured_never_pruned` both still pass against the rewritten baseline machinery.

### 10. Receipt hardcoded/path-prefix-derived claims (BLOCKER)

`canonical_credentials_store_excluded` was a hardcoded `True` regardless of manifest content; `database_snapshots_verified`/`flight_recorder_included` were counted by `archive_path` prefix match, not by anything actually proven.

**Repair:** `build_backup_receipt()` now derives `canonical_credentials_store_excluded` from whether the verified manifest's `exclusions[]` actually contains the canonical credentials record, and derives `database_snapshots_verified`/`flight_recorder_included` from `verify_agent_backup()`'s new `verified_sqlite_members` list (members that independently passed `_verify_sqlite_payload()`), not from path prefixes.

**Tests:** `test_backup_receipt_credential_exclusion_derives_from_manifest_not_hardcoded` (strips the exclusion record from an otherwise-valid archive and confirms the receipt now correctly reports `FAIL` rather than asserting an unproven claim), `test_backup_receipt_fields_are_real_not_fabricated` (updated to assert `database_snapshots_verified == 2`).

### 11. SQLite timeout semantics didn't match documented behavior (MEDIUM, fixed while the code was open)

A2R2 measured: configured `timeout_seconds=0.25`, actual block ≈5.01s. Root cause: `sqlite3.connect()` was never given an explicit `timeout=` argument, so it silently used the stdlib default 5-second busy_timeout at the C level — SQLite's own internal busy-handler retried for the full 5s *before* control ever returned to the Python-level progress callback that was supposed to enforce the 0.25s deadline.

**Repair:** both `sqlite3.connect()` calls in `_snapshot_sqlite()` (source and destination) now explicitly pass `timeout=timeout_seconds`, so the connection's own busy-handler retry budget matches the documented deadline instead of silently defaulting to 5s.

**Test:** `test_sqlite_connect_timeout_matches_configured_deadline_not_default_five_seconds` — holds a real exclusive lock via a second thread, configures `timeout_seconds=0.5`, and asserts the actual elapsed time stays under 3.0s (generous margin above 0.5s, comfortably below the old ~5s failure mode).

## Preserved, not rewritten (Section 14)

Confirmed still passing, unmodified in mechanism: SQLite online-backup snapshot consistency (`test_concurrent_writer_lock_releases_before_timeout_snapshot_waits_and_is_consistent`), immutable staging + hash-staged-bytes-not-live-bytes (`test_archive_hashes_staged_bytes_not_reopened_live_bytes`), ZIP-close-before-fsync ordering, verify-before-`os.replace()`, destination safety (`test_destination_inside_data_dir_rejected` et al.), existing-good-backup survival on build/verify failure, custom-tool quarantine semantics, test isolation (`test_tests_never_touch_real_machine_state`), ledger regenerate-only design.

## Self-adversarial pass (Section 16)

Thirteen of A2R2's highest-risk attacks independently re-run live against the repaired module, outside pytest (script: scratchpad `adversarial_pass.py`, run from `~/lumina-release`):

| Attack | Result |
|---|---|
| SQLite `lumina.db` hardlink credential leak | **Blocked** — trust-boundary check on the opened descriptor |
| SQLite `lumina.db` symlink credential leak | **Blocked** |
| Plain-file inode swap between `lstat` and `open` (TOCTOU) | **Blocked** — swap confirmed to actually fire, then confirmed excluded/no leak |
| Post-snapshot DB corruption | **Blocked** — independent `quick_check` on archived bytes catches it |
| Missing Flight Recorder | **Blocked** — build fails |
| Data-only Project state (DATA_DIR only, no BASE_DIR dir) | **Captured** (this one is a "should succeed" case, not an attack — confirmed working) |
| `major: true` / `major: 0` / `minor: -1` | **Blocked**, each independently |
| Missing canonical exclusions/regenerate records | **Blocked** |
| Unicode NFC-normalization namespace collision | **Blocked** |
| Custom tool with `restore_and_autoapprove` semantics | **Blocked** |
| Receipt asked to assert credential exclusion for an archive missing that record | **Blocked** — receipt reports `FAIL`, not a false `PASS` with a fabricated claim |

13/13 blocked (including the two SQLite variants counted separately).

## Verification (Section 17)

Focused suite: `python3 -m pytest tests/test_agent_backup.py -q`

```
93 passed, 1 warning in 8.4s
```

(The one warning is an expected `UserWarning: Duplicate name: 'manifest.json'` from `zipfile` itself, emitted by `test_verify_rejects_duplicate_zip_member_name` deliberately writing a duplicate entry to construct its fixture — not a defect.)

Full release suite: `python3 -m pytest -q`

```
3656 passed, 1 warning in 457.44s (0:07:37)
[exited with code 0]
```

Same expected warning as above (the deliberate duplicate-zip-entry fixture). `3656 = 3623 (A2F baseline) − 60 (old agent_backup suite) + 93 (new suite)` — confirms this rewrite replaced its own prior test file cleanly with zero regressions anywhere else in the 3.5k+ other tests.

## Remaining limitations (honest, not silently dropped)

- The SQLite credential-boundary re-check in `_snapshot_sqlite()` is a documented **narrower** mitigation than the plain-file fd-binding fix, for a real stdlib constraint (WAL sibling-file path resolution), not a shortcut — see Section 3's repair note above.
- The formal `VALIDLY_ABSENT/EMPTY/DISCOVERED/STAGED/SECURITY_REJECTED/FAILED` state enum from the mission's Section 5 was not introduced as literal code; the behaviors it describes were implemented directly against the four concrete gaps A2R2 found. A future task wanting machine-readable per-item collection state should be scoped separately.
- Directory-fsync durability remains best-effort, as before — not claimed as guaranteed on every filesystem/platform.
- Still no Restore, Restore UI, Backup UI wiring, credential encryption/export, or scheduler persistence — unchanged, out of scope.
- `metadata/shipped_baseline_hashes.json` is regenerated and present but still not wired into any release-build pipeline; keeping it current at future release cuts still requires someone to re-run `generate_baseline_hashes()` deliberately.

## STOP

No commit, push, or release→dev mirror occurred. Awaiting AGENT-BACKUP-RESTORE-A2R3.
