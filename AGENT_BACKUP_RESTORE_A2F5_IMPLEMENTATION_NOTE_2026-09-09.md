# AGENT-BACKUP-RESTORE-A2F5 — Implementation Note

**Status:** Repaired candidate ready for AGENT-BACKUP-RESTORE-A2R6. **Not committed, not pushed, not mirrored.** Per the mission's STOP-BEFORE-CHECKPOINT instruction, this only records what changed, why, and what remains.

## Repository gate (Section 0)

- Release (`~/lumina-release`, branch `main`): HEAD `ee40259` == `origin/main`, matches A2R5's own recorded historical baseline exactly. Working tree clean apart from this campaign's own untracked candidate files (all five `AGENT_BACKUP_RESTORE_A2F*_IMPLEMENTATION_NOTE_2026-09-08/09.md` files, the A1 design doc, `core/agent_backup.py`, `tests/test_agent_backup.py`, `metadata/`). No merge/rebase/cherry-pick/bisect state present.
- Dev (`~/lumina`, branch `dev-local`): HEAD `9b09542`, matches A2R5's recorded historical baseline exactly. Unchanged across this entire pass — not touched. `release-local` remote push remains structurally `DISABLED`, `push.default = nothing`, pre-push hook present and blocking.

## Findings repaired (Section 1)

### 1. Baseline exact-HEAD design self-destructs after checkpoint (BLOCKER, PRIORITY ZERO)

**Root cause:** A2F4's `_verify_baseline_against_git` required `baseline.git_commit == git rev-parse HEAD` exactly. A commit's SHA is a hash of its own tree, which includes every tracked file's bytes — including the baseline artifact's own `git_commit` field. The artifact can therefore never correctly declare the SHA of the commit that contains it: writing the correct value requires already knowing it, but writing it changes the tree, which changes the SHA. This is a genuine fixed-point impossibility, not an implementation gap. Bino caught this by inspection before A2R5 even ran; A2R5's own review independently confirmed it live.

**Repair — the provenance-anchor model (Sol's specification, Section 1-2):** [core/agent_backup.py:1465-1560](core/agent_backup.py:1465) `_verify_baseline_against_git` no longer treats `git_commit` as "the commit currently running." It is the **provenance anchor** the baseline's hash entries were derived from. Trust now requires ALL of:
1. The anchor commit resolves to a real commit (unchanged from A2F3/A2R3).
2. The anchor commit is an **ancestor of (or equal to)** the checkout's current HEAD (`git merge-base --is-ancestor`) — true for a legitimate release checkpoint (whose own commit necessarily descends from whatever anchor it declares) and for a true-ancestry dev-local mirror.
3. `lumina_version` matches (unchanged, checked by the caller before this function runs at all).
4. Every declared path's blob at the **anchor commit** matches the declared hash (closes A2R3's fabrication case, unchanged).
5. Every declared path's blob at **current HEAD** *also* matches the declared hash — new. If a later, legitimate commit changed a baseline-owned file, its current-HEAD blob no longer matches what the baseline (correctly, for the old commit) declared, and the whole baseline is rejected as stale.

Explicitly **not** a bare `merge-base --is-ancestor` relaxation, per Sol's own warning against it: step 5 is what prevents an arbitrarily stale same-version ancestor baseline from staying trusted forever after a real content change. Working-tree bytes are never consulted for either step 4 or step 5 — both read committed git objects via `git show <ref>:<path>` (`_git_show_bytes`, a small shared helper factored out of the duplicated logic), so a dirty working tree cannot redefine or invalidate an otherwise-valid baseline.

**Self-adversarial (live, outside pytest), re-running A2R5's own two proofs against the fix:**
- The exact disposable-repo checkpoint simulation A2R5 used to prove the self-destruct mechanism: commit A (release), generate baseline from A, commit B (checkpoint containing the artifact + candidate). Under the OLD model this failed authentication immediately; under the NEW model it now returns `TRUSTED=True, warning=None`.
- The exact read-only check against the REAL `~/lumina` dev tree A2R5 used to prove the bug was already live today (no future action needed): under the OLD model this returned `False` (HEAD mismatch); under the NEW model it still returns `False`, but now for a **genuinely correct** reason — `git diff` confirms `personas/discord_template.json` really did change between the release commit and dev's current HEAD (an added `"built by Bino"` clause). This is the model working as designed, not a residual bug: dev's real content has diverged, so honest rejection is the correct answer, not a false negative.

**Focused regression:** `test_baseline_ancestor_commit_with_unrelated_later_commit_still_authenticates` (Section 3 scenario C), `test_baseline_ancestor_commit_with_changed_baseline_owned_file_rejected_as_stale` (scenario D), `test_baseline_release_checkpoint_lifecycle_A_through_D` (the full A→B→C→D lifecycle in one disposable repo, matching Section 3's exact required simulation), `test_baseline_dev_descendant_lifecycle` (Section 3/24's dev-mirror scenario, plus a same-repo "dev then edits a baseline-owned file" follow-on). All pre-existing baseline regressions (`nonexistent_git_commit`, `different_real_commit`, `hash_algorithm_not_sha256`, `tampered_file_hash`, `malformed_files_map`, `unknown_overlay_path`, `missing_git_metadata_context`, `dirty_working_tree_does_not_invalidate`, `valid_artifact_matching_current_release_is_trusted`, `shipped_baseline_artifact_authenticates_through_full_load_path`) are unmodified and still pass — including the current `metadata/shipped_baseline_hashes.json` artifact, whose `git_commit` already equals current release HEAD (a trivial ancestor-of-itself case).

**No-git / packaged install (Section 4):** unchanged — the existing `not os.path.isdir(os.path.join(base_dir, ".git"))` early return still produces the correct `provenance = unknown + warning` fallback. No signing/packaging machinery was added.

### 2. Discovery-to-stage TOCTOU can silently drop required state (BLOCKER)

**Root cause:** A2F4 checked a required, state-bearing file's shape (regular? symlink? FIFO?) via a **separate, earlier** `lstat()` at discovery time (`_reject_if_special_file_or_symlink`, called during a `listdir`/`walk` loop or as an ad-hoc pre-check), then staged the bytes **later** through the ordinary lenient `_stage_plain_file`/`_open_verified` path. A2R5 raced a real persona in the gap between the two: swap to a symlink the instant after the discovery-time check passed, and the later staging layer's own (unrelated, unchanged) lstat-then-open sequence saw the symlink fresh and quietly excluded it with a warning — defeating Section 4/5's entire "discovered state must fail closed" guarantee.

**Repair — one atomic primitive, no separate check to race:** [core/agent_backup.py:855-953](core/agent_backup.py:855) `_open_state_bearing()` makes the shape decision and the forbidden-identity decision from a **single** `os.open()` + `fstat()` call — there is no earlier check for an attacker to win a race against. Never dereferences a symlink: `O_NOFOLLOW` makes `open()` itself fail with `ELOOP` on a symlink final component, atomically, as part of the same syscall that would otherwise have opened it. `O_NONBLOCK` is set alongside it so a FIFO can never hang this call waiting for a writer that will never come (harmless for a genuine regular file — POSIX only changes FIFO/socket/some-device read behavior under `O_NONBLOCK`, never a regular file's); a Unix domain socket path fails `open()` outright with `ENXIO` on Linux, also without blocking. Directory-escape detection resolves the descriptor's own canonical path via `/proc/self/fd/<fd>` when available (immune to a later rename of the pathname used to open it — the same technique `_connect_sqlite_with_bound_identity` uses), falling back to `os.path.realpath()` only when `/proc` is unavailable (matching pre-A2F5 behavior in that fallback case, not a new weakness).

[core/agent_backup.py:960-983](core/agent_backup.py:960) `_stage_required_state_file()` wraps this: any outcome other than a genuine, non-forbidden regular file — symlink, FIFO/socket/device/directory, a forbidden-identity alias, an I/O failure — raises `AgentBackupError` (SECURITY_REJECTED_STATE), never merely excludes-with-a-warning. Genuine, ordinary absence (`ENOENT`) returns `None`; the caller decides whether that's valid (an optional slot like `tool_audit.log`, never pre-enumerated) or must itself escalate (a file the caller already saw via `listdir`/`walk` moments ago, so a `None` now means it *vanished*, not that it never existed — `_collect_overlay_dir` and `_collect_custom_tool_members` both explicitly re-raise in that specific case).

**Wired into every Section 7 required-state collector**, replacing the old two-step pattern entirely: `_collect_preference_members` (prefs.json — not in Sol's Section 7 list explicitly, but a required-floor member; upgraded anyway for consistency and defense-in-depth rather than relying solely on the separate floor-presence check as an indirect safety net), `_collect_custom_tool_members`, `_collect_audit_members` (tool_audit.log, pending_actions.json, pending_actions_audit.log), `_collect_overlay_dir` (personas/skills/avatars/voices/tool_profiles), `_collect_project_members` (projectlist.md, project.md, binding.json, chats.json — `codebase.md` deliberately **not** upgraded, staying on the lenient path per its REBUILDABLE_GENERATED/optional exemption). SQLite sources (`lumina.db`/`flight_recorder.db`) are untouched — their own connection-bound fd-identity mechanism was separately hardened in A2F3/A2F4 and independently re-cleared by A2R5; Section 21/22 explicitly asks that it not be redesigned. `metadata.identity_trailers`/`metadata.shipped_baseline_hashes` (optional, non-required) stay on the lenient `_stage_plain_file` path, per Section 8's "irrelevant/optional → ignore/warn" carve-out.

The now-dead `_reject_if_special_file_or_symlink` (superseded, no longer called anywhere) was deleted rather than left as backwards-compatibility cruft.

**Focused regression:** `test_state_bearing_hardlink_swap_between_would_be_lstat_and_open_is_caught` (direct proof, via a spy on `os.lstat`, that the new primitive never calls it at all — there is no separate check left to race), plus the full Section 7 required-state race matrix (below). Four pre-existing tests that monkeypatched `_stage_plain_file` targeting personas/custom-tools were retargeted to `_stage_required_state_file` (two of the four — `test_archive_hashes_staged_bytes_not_reopened_live_bytes`, `test_source_replaced_with_symlink_after_staging_does_not_affect_archive` — had silently become **vacuous** without this fix: their monkeypatch simply never fired anymore once personas stopped calling `_stage_plain_file`, so they were "passing" without exercising anything; caught during this pass, not by their own unmodified assertions). `test_plain_file_hardlink_swap_between_lstat_and_open_is_still_caught` (the original A2R2 reproduction, proving the *lenient* path's fstat-bound decision) was retargeted from personas to `identity_trailers.json`, since personas no longer have a separate lstat for that race to occur against at all — a new companion test (`test_state_bearing_hardlink_swap_between_would_be_lstat_and_open_is_caught`) covers the strict path's own equivalent.

**Self-adversarial (live, outside pytest):** replayed A2R5's exact reproduction — sabotage the moment `_stage_required_state_file` is entered (i.e. as late as the sabotage can possibly fire before the real capture call runs), swapping the persona to a symlink. **Blocked** — `AgentBackupError`, sabotage confirmed fired, no archive published. Under A2F4 this identical setup published a "complete" backup with the persona silently missing.

### 3. Build-security evidence could be forged through the public receipt API (BLOCKER)

**Root cause:** A2F4's own fix for R4-B4 (the previous "build evidence vs. archive self-assertion" conflation) added `build_backup_receipt(archive_path, build_credentials_boundary_verified: bool | None = None)` — a public function with an ordinary boolean keyword parameter. A2R5 proved live that **any** caller, not just `build_agent_backup_with_receipt`, could pass `True` directly against a completely hand-built archive that never had a single real boundary check run against it, receiving a receipt that dishonestly claimed proven build-time evidence.

**Repair — a private, sentinel-guarded evidence object, never a public parameter:** [core/agent_backup.py:3389-3422](core/agent_backup.py:3389) `_BuildSecurityEvidence` requires a module-private `_EVIDENCE_SENTINEL` object to construct — never exported, never a documented parameter, never reachable by passing a boolean to anything. Not claimed as a cryptographic barrier (nothing in a shared Python process is truly unforgeable against other code with equal privilege in the same process, and the class's own docstring says so explicitly) — the real, narrower goal: an archive can never self-assert the claim (never read from manifest/archive bytes); no *public* caller can accidentally or carelessly mint it; future ordinary application code has no documented, discoverable way to enable it short of reaching into this module's private internals on purpose.

The public `build_backup_receipt(archive_path)` signature reverts to accepting **only** `archive_path` — the evidence parameter is gone entirely, not merely defaulted to a safe value. `build_agent_backup_with_receipt` is now the sole path that constructs evidence: immediately after its own `build_agent_backup` call succeeds, it reads the exact just-published bytes once (`_read_archive_bound_once`), computes their hash, and binds a `_BuildSecurityEvidence(archive_path, archive_sha256)` to precisely those bytes before handing everything to a new shared private constructor, `_build_receipt_from_bytes(archive_path, data, size, evidence)` — used by both the public and internal paths, so verify/hash/size still bind to one immutable capture (Section 16/17, unchanged from A2F4).

**Evidence-reuse binding (Section 15):** the receipt constructor only trusts evidence whose `(archive_path, archive_sha256)` matches the bytes it is *independently, separately* re-reading and re-hashing right now — a mismatch (reused evidence from a different archive, or the same path with different bytes) silently falls back to the honest `"not_provable_from_archive_alone"` rather than ever being trusted regardless.

**Focused regression:** `test_build_backup_receipt_public_signature_has_no_evidence_parameter` (proves the parameter is entirely gone — `TypeError`, not silently ignored), `test_hand_built_archive_cannot_forge_build_evidence_via_public_receipt_api` (the literal A2R5 reproduction, now blocked), `test_build_security_evidence_cannot_be_constructed_outside_the_module` (the sentinel guard itself), `test_build_evidence_reuse_across_different_archive_is_rejected` (Section 15's explicit three-case attack: wrong hash, wrong path, and the genuinely-matching case that IS trusted). `test_build_agent_backup_with_receipt_honestly_proves_build_time_boundary_check` (pre-existing, A2F4-era) is unmodified and still passes — the one legitimate path still works exactly as before.

### 4. ledger.db could be smuggled through a hardlinked state-bearing file (MEDIUM, release-gating per this mission)

**Root cause:** every trust-boundary identity check in the module (`_open_verified`, and by extension every collector) only ever compared a candidate file's opened-descriptor identity against `credentials_identity`. A2R5 proved live that hardlinking a persona filename to `ledger.db`'s inode caused ledger's real rows to be archived verbatim under the persona's own `archive_path` — violating A1's absolute "ledger.db is never staged, snapshotted, hashed, or archived" and this module's own "enforced by construction" claim.

**Repair — a trusted forbidden-identity set, not a single value:** [core/agent_backup.py:620-654](core/agent_backup.py:620) `_resolve_ledger_identity(data_dir)` mirrors `core/idempotency.py`'s own `LEDGER_PATH` (`DATA_DIR/memory/ledger.db`) without importing it, exactly the way `_resolve_credentials_identity` mirrors `core/secrets.py`. `_build_forbidden_identities(credentials_identity, ledger_identity)` builds a small labeled dict; `_open_verified` (lenient path) and `_open_state_bearing` (strict path) both now loop over this set instead of comparing a single value. Source-vetted per Section 9's own instruction for any *other* absolute never-archive source with one concrete, hardlink-aliasable file identity: `scheduled_tasks`/`worktrees`/`scratch_tmp` are EXCLUDED but are in-memory or whole-directory concepts, not a single file inode to alias this way — credentials and ledger are the only two that qualify, and both are now in the set.

For a **state-bearing** member, a forbidden-identity match now fails the WHOLE backup (via `_open_state_bearing`'s `'forbidden'` outcome), exactly like a symlink at the same position — this is itself a **policy escalation** from A2F4's "exclude just this one file with a warning," made because Section 9-10 explicitly required it for required, discovered positions ("Never: warn / rename logically / archive under another path"). For a genuinely optional member (`identity_trailers.json`), the lenient path is unchanged: still excludes just that one file with a warning.

**Section 11's "ordinary duplicate Agent state" case, decided:** two different logical archive positions sharing one physical inode (neither forbidden nor a shape violation) are simply captured independently, each under its own `logical_id`/`archive_path`, with identical (but independently read) bytes — no special detection, deduplication, or merging is attempted. Documented and tested explicitly (`test_ordinary_duplicate_agent_state_hardlink_captured_independently`) so this is a decided v1 semantic, not an accidental byproduct.

**Focused regression:** the full Section 7/9/11 hardlink race matrix (below) plus `test_persona_hardlinked_to_ledger_fails_whole_backup_no_leak` (the literal reproduction — confirmed no leak via `zipfile` inspection, not merely a return-code check), `test_custom_tool_hardlinked_to_ledger_fails_whole_backup`, `test_tool_audit_log_hardlinked_to_ledger_fails_whole_backup`, `test_project_binding_json_hardlinked_to_ledger_fails_whole_backup`, `test_project_chats_json_hardlinked_to_ledger_fails_whole_backup`. Two pre-existing A2F4-era tests that encoded the now-superseded "excludes just this one file" policy for a *state-bearing* credentials hardlink were rewritten to expect full failure (`test_state_bearing_hardlink_to_credentials_fails_whole_backup`, `test_state_bearing_credentials_hardlink_does_not_leak_via_persisted_partial_state`); a new test (`test_non_state_bearing_hardlink_to_credentials_still_excluded_not_leaked`) explicitly locks in that the lenient path's original behavior is unchanged for genuinely optional members, so the surviving distinction is tested, not merely asserted.

**Self-adversarial (live, outside pytest):** reproduced the exact A2R5 setup (persona hardlinked to a real ledger.db containing a distinctive marker row) directly against `build_agent_backup`. **Blocked** — `AgentBackupError`, no archive published, confirmed via direct `zipfile` inspection that no marker bytes exist anywhere in any published archive (there is none).

### 5. Unknown logical/member families could bypass trusted v1 policy (MEDIUM, release-gating per this mission)

**Root cause:** every existing fixed-value/allowlist check (`_validate_canonical_member_semantics`, `_validate_dynamic_member_policy`, `_validate_authority_bearing_member_schema`) only ever *enforced* policy for members whose `logical_id` matched a *known* pattern — a member matching **none** of them was silently skipped by every one of those checks and passed through as an ordinary, schema-valid member with zero v1 policy applied at all. A2R5 proved live that a member named `state.audit.totally_unconstrained_thing`, physically placed at `state/audit/random_extra.json` (a legitimately-allowed namespace prefix), verified `valid: True` and inflated the receipt's `rebind_required_count` via an attacker-chosen `portable: PORTABLE_WITH_REBIND`.

**Repair — closed-world membership, logical_id and archive_path validated together:** [core/agent_backup.py:2810-2854](core/agent_backup.py:2810) `_is_recognized_member(logical_id, archive_path)` requires the pair to correspond **exactly** to one of Lumina's known families — a fixed canonical member's `archive_path` must equal that member's own single fixed value; a dynamic family member's `archive_path` must equal exactly what that family's own `archive_path_deriver(logical_id)` function reconstructs. Nine dedicated deriver functions (`_deriver_custom_tools`, `_deriver_personas`, `_deriver_skills`, `_deriver_tool_profiles`, `_deriver_avatars`, `_deriver_voices`, `_deriver_project_binding`, `_deriver_project_chats`, `_deriver_project_md`, `_deriver_codebase_md`) each perform exact string reconstruction (prefix/suffix stripping, not delimiter-splitting — correctly handles a project name that itself contains dots, verified by a dedicated test) rather than a loose "looks plausible" heuristic; `_DYNAMIC_MEMBER_POLICY_FAMILIES`'s existing `(prefix, suffix, fixed_fields)` tuples gained a fourth element, the deriver, so there is one source of truth for "which family" rather than two independently-maintained tables. `_validate_closed_world_membership()` applies this to every physical member; a non-match invalidates the whole archive, not just that one member.

This closes Section 20 (receipt-count inflation) **automatically**, without any separate receipt-side code change: since the receipt is only ever built from a `report["valid"] is True` result, and an unrecognized member now makes that `False` unconditionally, an inflated count from an unconstrained member can no longer coexist with a passing receipt at all.

**Section 17's "validated together, not independently" requirement** is satisfied by construction — the deriver functions don't merely check that `archive_path` looks like a plausible member of the family; they reconstruct the exact expected value from `logical_id` and require byte-for-byte equality, so a member whose `logical_id` matches a real family but whose `archive_path` points at a *different* instance (e.g. one project's binding_json logical_id paired with another project's binding.json archive_path) is correctly rejected too (dedicated test).

**Section 18's near-match resistance** falls out of the same exact-derivation design: `binding.json.bak`, an embedded extra path segment (`chats.json/child`), a case-altered family prefix, and an appended suffix on a fixed logical_id (`pending_actions.json.evil`) all derive either `None` or a different expected path than what's actually declared, so none of them inherit a recognized family's policy.

**Focused regression:** `test_verify_rejects_unconstrained_member_the_literal_a2r5_reproduction` (the exact reproduction, both at `verify_agent_backup` and — critically — confirming `build_backup_receipt` reports `FAIL`, never an inflated `PASS`), parametrized `test_verify_rejects_near_match_dynamic_logical_ids` (four distinct near-match shapes), `test_verify_rejects_logical_id_archive_path_mismatch_within_a_real_family`, `test_is_recognized_member_accepts_every_real_family_shape` (exhaustive, one example per family — including the dotted-project-name edge case — directly guarding against an off-by-one in any deriver silently rejecting genuinely valid archives; this test caught two real bugs in its own first draft: a wrong assumed logical_id shape for `_pending` custom tools, verified against the actual collector's real output rather than assumed, and an invalid "near-match" case that was actually a legitimate — if unusual — filename edge case, corrected after checking rather than left as a false assertion).

## Required-state race matrix (Section 7), regression status

| Race | Persona | Skill | Custom tool | tool_audit.log | pending_actions.json | Project binding.json | Project chats.json |
|---|---|---|---|---|---|---|---|
| → symlink | pre-existing (A2F4) | pre-existing | pre-existing (rewritten this pass) | pre-existing | pre-existing | pre-existing | pre-existing |
| → FIFO | pre-existing | pre-existing | pre-existing | pre-existing | — (same primitive, same code path as symlink; not independently re-tested per file type, see note below) | — | pre-existing |
| → socket | pre-existing | — | — | — | — | — | — |
| → credentials hardlink | **new, rewritten this pass** | — | — | — | — | — | — |
| → ledger hardlink | **new this pass** | — | **new this pass** | **new this pass** | — | **new this pass** | **new this pass** |
| → deletion during staging | pre-existing | — | pre-existing | — | — | — | — |

Every cell not independently re-tested per exact file type shares the identical code path (`_open_state_bearing`) already proven for the cells that are — the mechanism is generic across all seven collectors by construction (see `_stage_required_state_file`'s own callers), not re-implemented per member type. Cells left "—" were judged already adequately covered by the mechanism's genericity plus at least one direct test elsewhere in the matrix, rather than exhaustively multiplying near-identical test bodies; this is a scope judgment, recorded here rather than silently made.

## Self-adversarial pass (Section 25)

Eleven attacks independently re-run; the highest-priority ones (baseline post-checkpoint, baseline dev-descendant, discovery-to-stage TOCTOU, ledger hardlink) were run **live, outside pytest**, re-executing A2R5's own exact reproduction scripts against the repaired code; the rest confirmed via the focused suite's dedicated regression tests named above under each finding.

| Attack | Result | Evidence |
|---|---|---|
| Post-checkpoint baseline simulation | **Fixed** | live script — A2R5's exact disposable-repo reproduction now returns `TRUSTED=True` |
| Dev-descendant baseline simulation | **Fixed, correctly** | live read-only check against real `~/lumina` — now rejects for a genuine content difference (verified via `git diff`), not the structural bug |
| Stale ancestor with changed baseline-owned file | **Blocked** | focused test (`test_baseline_ancestor_commit_with_changed_baseline_owned_file_rejected_as_stale`) |
| Discovery-to-stage TOCTOU (regular→symlink) | **Blocked** | live script — A2R5's exact race, sabotage confirmed fired, full backup rejected |
| Regular→ledger-hardlink race | **Blocked** | live script + focused test (5 collectors) |
| Regular→credentials-hardlink race (state-bearing) | **Blocked** | focused test (2 tests, rewritten from A2F4) |
| Public receipt build-evidence forgery | **Blocked** | focused test — `TypeError`, parameter no longer exists |
| Evidence reuse across different archive | **Blocked** | focused test (3 sub-cases: wrong hash, wrong path, genuine match) |
| Unknown logical member under valid namespace | **Blocked** | focused test — the literal reproduction, receipt confirmed `FAIL` not inflated `PASS` |
| Near-match dynamic logical IDs | **Blocked** | focused test (4 parametrized shapes) |
| Receipt-count inflation | **Blocked** | closed automatically by the closed-world fix; confirmed via the same reproduction test |

11/11 blocked or fixed-and-verified.

## Focused tests (Section 26)

```
python3 -m pytest tests/test_agent_backup.py -q
```

```
214 passed, 1 warning in 22.23s
```

The one warning is the same expected `UserWarning: Duplicate name: 'manifest.json'` from `zipfile` itself (`test_verify_rejects_duplicate_zip_member_name`'s deliberate fixture) — not a defect, unchanged from every prior pass.

`214 = 192 (A2R5-independently-confirmed A2F4 baseline) + 22` new/changed regression items across the five findings above.

## Full release suite (Section 27)

A2R5's own run of this gate completed cleanly (3755 passed, 483.61s), unlike A2R3/A2R4's prior stalls at `tests/test_review_panel.py`. This run was launched fresh, in the background, and allowed to run to completion with no intervention:

```
python3 -m pytest -q
```

```
3777 passed, 1 warning in 473.54s (0:07:53)
[exited with code 0]
```

`tests/test_review_panel.py` passed cleanly (32/32) — the third consecutive clean run through that historical stall point (A2F3, A2F4, and now this one), further reinforcing that it is pre-existing, unrelated suite nondeterminism (`project_full_suite_intermittent_deadlock` memory), not something this campaign contributes to.

`pytest --collect-only -q` immediately afterward reports **3777 tests collected**, exactly matching the **3777 passed** above — no discrepancy.

The same expected `UserWarning: Duplicate name: 'manifest.json'` as every prior run, from the same deliberate fixture — not a defect.

`3777 = 3755 (A2R5-independently-confirmed baseline) + 22` — exactly matching the focused suite's own `+22` delta (`192 → 214`), confirming the only change to the total suite size across this pass came from `test_agent_backup.py` itself.

## Duck (Section 24)

Re-verified from disk, not assumed: `sha256sum skills/rubber-duck-debugging.md` = `d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be` — an exact match to Sol's corrected (full, 64-character) value. Present, unchanged, `git status --short` reports nothing for it (untouched by this pass), `.gitignore:39` still covers it, and it remains correctly absent from `metadata/shipped_baseline_hashes.json`'s own `files` map (so its provenance still classifies `unknown`, never `shipped_baseline_unmodified`, per every prior pass's own behavior — unchanged by this pass's baseline-model rewrite, since the provenance-anchor model changes *how* a claimed entry is authenticated, not *which* files a baseline is allowed to claim). No duck-related code was touched.

## STOP

After implementation and verification: no commit, no push, no mirror, no cleanup beyond what's described above. Returned as a candidate for **AGENT-BACKUP-RESTORE-A2R6**.
