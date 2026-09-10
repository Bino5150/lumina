# AGENT-BACKUP-RESTORE-A2F8 — Implementation Note

**Status:** Repaired candidate ready for AGENT-BACKUP-RESTORE-A2R9. **Not committed, not pushed, not mirrored.**

## Repository gate (Section 0)

- Release (`~/lumina-release`, branch `main`): HEAD `ee40259c980d3c6fcca56fc2f27dcefd3814c178` == `origin/main`, matches the mission's recorded historical A2R8 baseline exactly. No merge/rebase/cherry-pick/bisect state present, no stray `.git/*.lock`. Working tree at start: this campaign's own untracked note/design files plus the uncommitted `core/agent_backup.py`/`tests/test_agent_backup.py`/`metadata/` from A2F through A2F7 — all preserved as-is, per [[feedback_lumina_handoffs_patches_are_storage]].
- Dev (`~/lumina`, branch `dev-local`): HEAD `9b09542489210445b6b7fcc4ef2148e5f9eb64a8`, matches the mission's recorded historical A2R8 dev baseline exactly. Untouched by this pass. `release-local` remote push remains structurally `DISABLED`; its executable push-blocking hook (`"Push blocked: ~/lumina is a local-only development repository."`) is present and was not modified. Release HEAD `ee40259` confirmed an ancestor of dev HEAD `9b09542` (dev has already absorbed this exact release commit via a prior mirror).
- `push.default` unset (repository default) on release; release has no pre-push hook (owner pushes manually, as expected — [[feedback_checkpoint_and_push_authorization]]).

## Preserved A2R8 PASS areas (Section 1)

Confirmed via the full focused/full suite runs below, not assumed: universal physical-file strict capture, `st_nlink == 1`, stable double-read/fstat capture, bounded retry, rotated credential/ledger alias rejection, SQLite connection identity, SQLite online snapshot consistency, baseline Git lifecycle semantics (release-checkpoint/dev-descendant/stale-owned-file scenarios), publication content observation, public receipt build-evidence separation, closed-world logical/path grammar, legitimate names, malformed-verifier never-raises, publication preservation before replacement, test isolation, duck.

## B1 — Verification and immutable capture used different archive snapshots (BLOCKER)

**Root cause:** `_build_agent_backup_core` called `verify_agent_backup(tmp_path)` — which opens `tmp_path` **by name**, verifies it, and closes its own handle internally — and only *then*, in a separate later statement, reopened `tmp_path` **by name again** (`os.open(tmp_path, os.O_RDONLY)`) to capture `built_data`/`built_sha256`/`built_size`. A2R8 proved the gap between those two opens is real: swap `tmp_path`'s pathname to a *different*, independently verifier-valid archive B in that window, and B silently inherits A's already-completed PASS verdict — `built_data` describes B, the returned `manifest` describes A (from the pre-serialization Python dict, not from what was actually verified), and nothing ever actually verified B's own bytes at all.

**Architectural repair:** [`_capture_and_verify_archive`](core/agent_backup.py:2894) is the new single choke point: it captures a target path's exact bytes via the universal stable-capture boundary (`_open_and_capture_stable_bytes` — the same fstat/read/fstat bracketed double-read proof and bounded retry every other physical source in this module already uses), then strictly verifies **those exact bytes** via `verify_agent_backup(io.BytesIO(data))` — never a second, separate reopen of the path. There is exactly one `os.open()` of the target path in this function; verification runs against the bytes that one read produced, never against the pathname a second time. `_build_agent_backup_core` now calls this once against `tmp_path` immediately after the fsync (replacing the old verify-then-reopen sequence), and its returned `report["manifest"]` — not the pre-serialization dict — becomes `_BuiltArchiveResult.manifest`, so every historical field this function returns (`manifest`, `built_data`, `built_sha256`, `built_size`) is now derived from the **one** immutable captured object (Section 4's explicit requirement). `tmp_path` is deleted immediately after this call and is never touched again for any reason, including publication (see B3's Section 6 redesign below) — closing even the residual "capture built_data, then still rename tmp_path itself for publication" window a narrower fix would have left open.

`verify_agent_backup`'s signature/docstring is updated to state explicitly that it accepts a path *or* a bytes-like/file-like source (`io.BytesIO`) — it already delegated to `_verify_agent_backup_inner`, which already supported this; only the public contract needed to catch up to match. `_capture_and_verify_archive` deliberately calls the **module-level** `verify_agent_backup` (never `_verify_agent_backup_inner` directly) so it remains the same monkeypatchable, never-raises seam every other caller already uses — confirmed by `test_existing_good_destination_survives_a_verification_failure`, which monkeypatches `ab.verify_agent_backup` directly and must still intercept the build path's own verification call.

**Regression:** `test_b1_capture_and_verify_archive_opens_target_path_exactly_once` (the literal reproduction — proves exactly one `os.open()` of the captured path, and that a hypothetical reopen would be visibly caught by a bytes/hash mismatch rather than silently passing), `test_b1_build_result_manifest_derives_from_the_same_captured_bytes` (the returned manifest is byte-for-byte identical to `manifest.json` as it exists inside the published archive).

**Live adversarial replay (outside pytest):** built two independently-valid archives A and B; hooked `os.open` to swap the target path's content to B on what *would* be a second open; confirmed `_capture_and_verify_archive` opens the path exactly once, returns A's bytes/hash/manifest self-consistently, and B is never touched.

**Remaining limitation:** none specific to this finding — capture and verification are now structurally the same operation on the same bytes; there is no code path left that could reintroduce the gap without a return-type change reviewers would immediately notice.

## B2 — Baseline archived payload and control/provenance evidence used different snapshots (BLOCKER)

**Root cause:** `_collect_metadata_members` staged `shipped_baseline_hashes.json`'s bytes into the archive via the universal capture boundary (one open, archived once) — but `_build_agent_backup_core` separately called `_load_baseline_hashes(baseline_hashes_path, lumina_version, base_dir)`, which reopened the **live** `baseline_hashes_path` **by name a second time** to parse it for the provenance decisions (`shipped_baseline_unmodified`/`modified`/`unknown`) applied to every other `USER_OVERLAY` member (personas, skills, avatars, voices, tool_profiles, projectlist.md). A2R8 proved a baseline swapped between those two opens lets an archived, Git-authenticated A coexist with a completely different B actually driving every other member's provenance classification.

**Architectural repair:** [`_capture_and_stage_physical_state_file`](core/agent_backup.py:1360) is a new sibling of `_stage_physical_state_file` that returns the captured bytes *alongside* the staged path (every other physical member stays on the plain, staged-path-only variant — `_stage_physical_state_file`'s body is now a one-line wrapper over this function, so no other call site changed behavior at all). `_collect_metadata_members` uses it *only* for `shipped_baseline_hashes.json` — the one physical member this module's own source-vetted audit (Section 11, below) confirms is genuinely dual-use — and now returns `(members, baseline_snapshot_data)` instead of a bare member list; `_collect_members` propagates the tuple. `_load_baseline_hashes`'s parsing/authentication logic is factored out into [`_parse_baseline_hashes(raw_bytes, expected_lumina_version, base_dir, source_label)`](core/agent_backup.py:2043), which operates on already-captured bytes; `_load_baseline_hashes(path, ...)` is now a thin wrapper (open once, delegate) kept for standalone/direct callers (and the existing direct test `test_shipped_baseline_artifact_authenticates_through_full_load_path`, which calls it by path deliberately). `_build_agent_backup_core` now calls `_parse_baseline_hashes` directly with the bytes `_collect_members` already captured — `baseline_hashes_path` is **never reopened by name** anywhere in a build.

**Section 11 audit (dual-use physical inputs), independently re-verified:** grepped every physical member collector for a source read a second time for anything other than archiving. `shipped_baseline_hashes.json` is confirmed as the only one: archived *and* parsed for provenance control. Every other physical member's own already-computed `sha256 = _sha256_file(staged)` (from its own one staged capture) is only ever *compared against* the baseline dict's stored hash — that comparison never reopens the member's own live source a second time, so it isn't a dual-use case. `_read_lumina_version(base_dir)` reads `main.py` for a version string but never archives `main.py` at all (a different file, single-purpose read, not dual-use). Git objects (`_git_show_bytes`, used by `_verify_baseline_against_git`) authenticate the baseline's own claims from immutable commit history, never from a working-tree file this module also archives. No other dual-use physical input exists; Section 11's prior conclusion holds.

**Regression:** `test_b2_baseline_path_opened_at_most_once_and_archive_matches_provenance` (the literal reproduction — swaps the baseline file at what would be a second open, confirms at most one open occurs, and that the archived baseline payload and every other member's provenance decision always agree), `test_b2_load_baseline_hashes_standalone_entry_point_still_works` (the refactored standalone path-based entry point still behaves identically for non-build callers).

**Live adversarial replay (outside pytest):** built a real git-backed baseline A; hooked `os.open` (via `_open_and_capture_stable_bytes`'s own `os.open` call) to swap `baseline_hashes_path`'s content to a `hash_algorithm="md5"` variant B on what would be a second open; confirmed the path is opened at most once, and the archived baseline payload's `hash_algorithm` and the resulting provenance classification always agree with each other (never a hybrid where the archive says A but classification used B).

**Remaining limitation:** none specific to this finding.

## B3 — Rollback trusted a mutable, unverified recovery pathname (BLOCKER)

**Root cause:** A2F7's own fix for the A2R7 Section 26 finding preserved the prior destination via a bare `os.link(dest_path, dest_path + ".a2f7_prior_good.tmp")` — a **predictable, reusable sibling pathname** — and, on a detected publication mismatch, restored it with a plain `os.replace()`. No expected hash, no re-verification of the preserved content, no identity proof, and no post-restore observation. Deeper still: a hardlink shares the *same inode* as whatever `dest_path` held before — if that prior inode were mutated in place (same-inode rewrite) at any point before the rollback actually fired, the "recovery" copy would silently reflect the tampered bytes too, since it was never an independent, verified snapshot in the first place.

**Architectural repair:**
- **Capture-once, verify-before-trust, for the existing destination too.** Before a build is allowed to touch `dest_path` at all, if it exists, `_build_agent_backup_core` now calls the *same* `_capture_and_verify_archive` used for the freshly-built candidate (Section B1) against `dest_path` itself. If that capture or strict verification fails — an unverifiable, unknown, or actively-corrupted existing destination — the whole build raises **before any publication step runs**, leaving `dest_path` completely untouched (Section 13: "preserving an unknown existing backup is preferable to publishing over it without recoverable rollback"). This is a deliberate, stricter v1 policy than A2F7's: previously *any* existing content at `dest_path` was silently overwritten (with only a best-effort hardlink as a hedge); now an unverifiable prior destination blocks the build outright.
- **Rollback authority is immutable bytes/hash held in memory, never a filesystem pathname.** `prior_snapshot = (prior_data, prior_sha256)` — plain Python values, captured once, before publication even begins. Nothing in the rollback path ever names, reopens, or trusts a *pathname* as the source of truth.
- **One shared publish-and-confirm mechanism for both directions.** [`_publish_immutable_bytes`](core/agent_backup.py:2945) writes already-realized, immutable bytes to a **fresh, private, randomly-named** temp file (`tempfile.mkstemp(prefix=".agent_backup_pub_", ...)` — never a predictable/reusable name), fsyncs it, atomically replaces `dest_path`, best-effort fsyncs the directory, and then independently re-observes `dest_path` **by content** (never merely by inode identity — A2F7's own Section 18 lesson) before ever reporting success. Forward publication of the new build and rollback republication of the immutable prior snapshot both call this exact function — one evidence-producing mechanism, not two independently-written, potentially-divergent implementations of "write bytes, verify they landed."
- **Rollback failure preserves a recovery artifact, never silently loses the prior bytes.** If the rollback's own `_publish_immutable_bytes` call fails — either a content-mismatch (`AgentBackupError`) or an infrastructure failure (`OSError` from the write/fsync/replace itself) — [`_preserve_rollback_failure_artifact`](core/agent_backup.py:3027) writes the immutable prior bytes to a fresh, clearly-labeled (`.agent_backup_ROLLBACK_FAILED_RECOVERY_*`), randomly-suffixed file and the raised error names both that path and the expected sha256. This file is a local variable only — never wired into any generic cleanup path in this module, and its name is never treated as authoritative by any later automated decision (Section 16's explicit requirement).

**Section 6's own broader question, adopted:** once `built_data` exists as an immutable object, publication no longer depends on `tmp_path`'s own inode at all — `_publish_immutable_bytes` always writes from the immutable bytes to a brand-new temp, closing even the residual "capture, then still rename the original staging file" window. `_BuiltArchiveResult.built_identity` is retained (for API-shape stability; no caller reads it) but now describes the *publication* temp's identity rather than the staging temp's — purely informational, since publication correctness is now addressed by content hash, never by identity.

**Regression:** `test_b3_rollback_leaves_no_predictable_or_reusable_recovery_pathname` (confirms no fixed-name file of any kind survives a full sabotage-and-rollback cycle), `test_b3_rollback_authority_is_in_memory_bytes_not_a_filesystem_artifact` (rollback still succeeds correctly even when every stray temp file in `dest_dir` is deleted immediately before the publishing replace lands — proving the authority is the in-memory snapshot, not a filesystem artifact), `test_b3_rollback_failure_preserves_recovery_artifact_with_expected_hash` (forces both the forward publish *and* the rollback itself to fail; confirms a recovery artifact appears on disk with exactly the prior good bytes, and the error names its path and expected hash), `test_b3_existing_unverifiable_destination_blocks_build_and_is_preserved` (a garbage, non-archive `dest_path` blocks the build entirely and survives completely untouched), `test_b3_existing_valid_destination_is_captured_and_replaced_normally` (the ordinary non-adversarial rebuild-over-a-valid-backup case still succeeds). The two pre-existing A2F7 rollback tests (`test_publication_mismatch_restores_the_prior_good_destination`, `test_publication_mismatch_with_no_prior_destination_reports_honestly`) are **unmodified and still pass** — the new mechanism satisfies the exact same observable contract (`"prior destination restored"` / `"no prior destination existed to restore"` in the raised error) via a completely rebuilt internal implementation.

**Live adversarial replay (outside pytest):** the predictable-pathname attack (mutable rollback snapshot replacement/deletion, as literally named in the mission) is no longer applicable by construction — there is no predictable name to replace or delete; replayed the closest faithful equivalent instead (deleting every stray temp file in `dest_dir` mid-flight) and confirmed rollback still succeeds from the in-memory snapshot alone.

**Remaining limitation, stated honestly:** if the rollback's own fresh publication temp is itself mutated **in place**, same-inode, in the narrow window between `_publish_immutable_bytes`'s own write+fsync and its post-replace re-observation, that mutation is caught by the same content-hash re-observation check that catches everything else in this module (the failure would simply be reported as a second-level rollback failure, triggering `_preserve_rollback_failure_artifact`) — this is not a new gap, it is the same irreducible, honestly-documented residual window `_publish_immutable_bytes` already carries for ordinary forward publication (Section 11/19 of A2F7's own note), now shared symmetrically by both call sites rather than being unique to one. Separately, and by explicit design rather than oversight: capturing and strictly verifying an existing destination now means **fully reading and re-verifying the entire prior archive on every rebuild**, not merely stat-ing it — a real, accepted I/O/CPU cost for large archives, the same category of cost Section 29/[[project_agent_backup_restore_a1_status]]'s AGENT-BACKUP-SPARE-LARGEFILE-01 already tracks and this pass was explicitly told not to let block B3's correctness fix.

## B4 — Strict v1 accepted ZIP prefixes / concatenated archives (BLOCKER)

**Root cause:** the existing trailing-bytes check (A2R7/Section 20) proved an archive's raw bytes exactly account for themselves **after** its own End-Of-Central-Directory record, but never proved the archive's **first** local file header starts at byte zero. Python's `zipfile` (like most real ZIP readers) locates the EOCD by searching backward from the end of the file and transparently adjusts every entry's `header_offset` for whatever precedes it — exactly the behavior that makes a self-extracting-stub-shaped leading prefix, or a second, complete ZIP archive concatenated in front of Lumina's own, invisible to a check that only inspects `zipfile`'s already-parsed member list.

**Source-vetted builder grammar (Section 20):** empirically confirmed (not assumed) `build_agent_backup`'s own `zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)` always emits: `compress_type == 8` (DEFLATE) for every member regardless of compressibility, `flag_bits == 0` (no data descriptors, no encryption — writing to a real seekable file with sizes known up front), `extract_version == 20` (no ZIP64), the first member's `header_offset == 0`, and a single EOCD with a zero-length comment. This is the exact, narrow subset the new structural check validates — deliberately not a general ZIP parser.

**Architectural repair:** [`_validate_zip_physical_structure`](core/agent_backup.py:4114) independently walks the archive's own physical local file headers (via [`_parse_local_header`](core/agent_backup.py:4081), a defensive, never-raising byte-level parser) using `zipfile`'s own **prefix-adjusted** `header_offset` values (confirmed empirically to reflect true physical position, not the raw value stored in the central directory) and requires them to exactly tile `[0, cd_offset)`:
- the first local header must start at byte 0 (closes the leading-prefix attack and, independently, the concatenated-archive attack — for a concatenated A+B, `zipfile` finds and parses only the *last* EOCD, i.e. B's, whose own real members' adjusted offsets start at `len(A)`, not 0);
- the central directory's raw, self-reported `cd_offset` must equal `idx - cd_size` (where `idx` is the physically-found EOCD position) — a second, independent confirmation of "no unaccounted prefix," derived without trusting `zipfile`'s own adjustment logic;
- every declared member's local record must be found at exactly the offset the central directory declares, and every subsequent member must begin exactly where the previous one's header+payload ended — no gap, no overlap, no unreferenced extra local record squeezed in between legitimate ones or before the central directory;
- the last accounted-for local record must end exactly where the central directory begins;
- multi-disk/split archives and ZIP64 extensions (never emitted by the builder) are rejected outright by their EOCD sentinel values, rather than partially interpreted.

Per-member local/central-directory agreement (Section 22-23) is checked alongside the tiling: filename (raw bytes, compared using the same UTF-8-or-cp437 rule `zipfile` itself used to decode the central directory's copy — never a lossy decoded-string comparison), compression method, compressed size, and the absence of the streaming-data-descriptor flag bit. This closes the "local header declares a different physical member than the central directory" class of ambiguity a future Restore (or this verifier's own reads) could otherwise be induced into.

Wired into [`_verify_agent_backup_inner`](core/agent_backup.py:4290) immediately after the existing duplicate-name check, using the archive's raw bytes (already available via the existing `_raw_bytes_for_trailing_check`) and `zf.infolist()`; wrapped so a parse failure reports a structured error rather than raising (Section 25).

**Regression:** `test_b4_leading_prefix_bytes_rejected`, `test_b4_single_leading_byte_rejected` (the minimal one-byte case), `test_b4_concatenated_archives_rejected` (both orderings, A+B and B+A), `test_b4_extra_unreferenced_local_record_rejected` (a real local header + payload spliced in, with the EOCD's `cd_offset` field patched so every *legitimate* member still parses perfectly around it — sanity-checked that `zipfile` itself and `testzip()` see nothing wrong, proving only the new tiling check catches it), `test_b4_local_central_filename_mismatch_rejected`, `test_b4_multi_disk_zip_rejected`, `test_b4_zip_comment_still_rejected_alongside_new_structural_checks` and `test_b4_genuine_builder_archive_still_verifies_valid` (regression guards confirming the pre-existing trailing-bytes/comment check and the ordinary success path are both unaffected).

**Live adversarial replay (outside pytest):** all eight ZIP-structure constructions above run against real `build_agent_backup`-produced archives (never hand-typed manifests) — leading garbage, concatenation in both orders, and a precisely-constructed "extra unreferenced local record" (central directory patched to remain internally self-consistent around it) all rejected with the expected, specific error messages; a genuinely untampered archive still verifies clean.

**Remaining limitation:** this check validates exactly the structural subset `build_agent_backup`'s own writer emits; it is not a general-purpose ZIP-bomb or archive-fuzzing defense, and does not attempt to validate ZIP64 or multi-disk archives beyond rejecting them outright (consistent with [[project_agent_backup_restore_a1_status]]'s AGENT-BACKUP-SPARE-LARGEFILE-01 follow-up, which remains a memory/IO-cost item, not a structural-verification one).

## Self-adversarial replay, outside pytest (Section 31)

Run via a standalone script (retained in this session's scratchpad, not committed) against the actual module in this checkout — 20 checks, all pass:

| Attack | Result |
|---|---|
| B1: verify A → swap B → immutable capture | **Blocked** — exactly one `os.open()`, bytes/hash/manifest all describe A |
| B2: baseline archived A vs. control B | **Blocked** — baseline path opened at most once; archived payload and provenance decision always agree |
| B3: mutable rollback snapshot replacement/deletion | **N/A by construction** (no predictable pathname exists); faithful equivalent (temp-file deletion pressure mid-flight) — rollback still succeeds from the in-memory snapshot |
| B3: rollback same-inode mutation | Covered by the shared content-hash re-observation in `_publish_immutable_bytes`, exercised via the same mechanism as ordinary publication tampering |
| B4: leading-garbage ZIP | **Rejected** |
| B4: concatenated ZIP A+B | **Rejected** |
| B4: extra local record | **Rejected** |
| Unknown-unknown pass: trailing bytes, ZIP comment, and local/central filename mismatch still rejected after the B4 refactor; ordinary build→receipt success path unaffected; rebuilding over an existing valid backup still succeeds; an unverifiable existing destination now blocks the build and survives untouched | **All confirmed** |

## Focused suite (Section 32)

```
python3 -m pytest tests/test_agent_backup.py -q
```

```
294 passed, 1 warning in 31.80s
```

The one warning is the same expected `UserWarning: Duplicate name: 'manifest.json'` from `zipfile` itself (the deliberate `test_verify_rejects_duplicate_zip_member_name` fixture) — not a defect. `294 = 277 (A2F7's own confirmed baseline) + 17` net-new tests (2 for B1, 2 for B2, 5 for B3, 8 for B4). No test removed, none renamed — every A2F7 test is unmodified and still passing.

## Complete release suite (Section 33)

A fresh, uninterrupted background run against this exact final code:

```
python3 -m pytest -q
```

```
3857 passed, 1 warning in 507.37s (0:08:27)
[exit code 0]
```

`tests/test_review_panel.py` passed cleanly (32/32) — another clean pass through that historical PySide6/pytest-teardown stall point (see [[project_pyside6_pytest_teardown_segfault]]), no native crash.

`pytest --collect-only -q` immediately afterward reports **3857 tests collected**, exactly matching **3857 passed** — no discrepancy. `grep -c "FAILED\|ERROR"` on the raw log: **0**.

The same expected `UserWarning: Duplicate name: 'manifest.json'` as every prior run, from the same deliberate fixture — not a defect.

`3857 = 3840 (A2F7's own confirmed full-suite baseline) + 17` — exactly matching the focused suite's own `+17` delta (`277 → 294`), confirming the only change to the total suite size across this pass came from `test_agent_backup.py` itself.

## Duck (Section 30)

Re-verified from disk, not assumed: `sha256sum skills/rubber-duck-debugging.md` = `d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be` — exact match against the mission's pinned value. Present, unchanged, untracked, not shipped baseline, provenance unknown. No duck-related code touched. Nobody touched the fucking duck.

## The four load-bearing architectural statements (Section 34, explicit)

1. **Verify the immutable captured archive, never a pathname that is later recaptured.** `_capture_and_verify_archive` opens its target exactly once and verifies those exact bytes; there is no `verify(path) -> reopen(path)` boundary anywhere in this module's build path.
2. **Any physical file used as both payload and control evidence is captured once and reused.** `shipped_baseline_hashes.json` (the one confirmed dual-use member, per Section 11's re-audited conclusion) is captured once via `_capture_and_stage_physical_state_file`, and those exact bytes drive both the archived payload and the provenance-classification decision — never a second open of the live path.
3. **Rollback authority is immutable prior bytes/hash, never a mutable recovery pathname.** `prior_snapshot` is an in-memory `(data, sha256)` tuple, captured and strictly verified before publication begins; `_publish_immutable_bytes` always writes from immutable bytes to a fresh, randomly-named, single-use temp — there is no predictable or reusable recovery filename anywhere in this module.
4. **Strict v1 ZIP verification accepts only the single unambiguous physical structure Lumina's own builder can emit.** `_validate_zip_physical_structure` requires the first local header at byte 0 and an exact, gap-free, overlap-free tiling of every physical byte between there and the central directory — no prefix, no concatenated archive, no unreferenced record, no unexplained data, scoped deliberately to the narrow structural subset `zipfile.ZipFile(..., ZIP_DEFLATED)` actually emits rather than general ZIP creativity.

## STOP

After implementation and verification: no commit, no push, no mirror, no cleanup beyond what's described above. Returned as a candidate for **AGENT-BACKUP-RESTORE-A2R9**.
