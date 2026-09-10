# AGENT-BACKUP-RESTORE-A2F9 — Implementation Note

Ninth repair pass. AGENT-BACKUP-RESTORE-A2R9 found a single unifying theme
across four BLOCKERs and one HIGH/release-gating finding: A2F8 closed every
seam between two operations that each independently *opened the same archive
path* — but left open a wider, more general version of the identical mistake
one level down, in how every ordinary *member* got from its source into that
archive in the first place. This note records root cause, architectural
repair, regression coverage, live adversarial replay, and remaining
limitation for each finding, then the focused/full suite results.

## Repository and process gate (Section 0)

- Release (`~/lumina-release`, `main`): HEAD `ee40259c980d3c6fcca56fc2f27dcefd3814c178`,
  `origin/main` identical, working tree clean apart from this campaign's own
  untracked deliverables (`AGENT_BACKUP_RESTORE_*` notes, `core/agent_backup.py`,
  `tests/test_agent_backup.py`, `metadata/`) — matches the A2R9 baseline
  exactly. No in-progress git operation (no `MERGE_HEAD`/`REBASE_HEAD`/
  `CHERRY_PICK_HEAD`/`rebase-merge`/`rebase-apply`/`sequencer` state present).
  `push.default` unset (git default `simple`); no `pre-push` hook in this repo.
- Dev (`~/lumina`, `dev-local`): HEAD `9b09542489210445b6b7fcc4ef2148e5f9eb64a8`,
  matches the A2R9 baseline exactly; release HEAD confirmed an ancestor of dev
  HEAD (`git merge-base --is-ancestor` — true). `release-local` remote push
  URL is the literal string `DISABLED`; `~/lumina/.git/hooks/pre-push` prints
  `"Push blocked: ~/lumina is a local-only development repository."` and
  exits 1 unconditionally — dev push remains structurally disabled.
- PID `1897170`: confirmed by full command line (`python3 -m pytest -p
  no:cacheprovider -q`), cwd (`/home/bino/lumina-release`), parent shell's own
  command line (`task_tmp=$(mktemp -d /tmp/lumina-a2r9-full.XXXXXX)`, the
  `A2R9_FULL_*` marker names), and thread state (every one of its 15 threads
  parked in `futex_do_wait`, one holding open a `flight_recorder.db`/`-wal`/
  `-shm` triple under `test_context_history_shaped_pa0` — the same stall
  signature A2R9 itself reported) to be exactly the described stale, already-
  incomplete A2R9 full-suite run. Sent `SIGTERM`; the process group exited
  gracefully within 5 seconds — no `SIGKILL` was needed, and no unrelated PID
  was touched.

## Preserved A2R8/A2R7/… PASS areas (Section 1)

Full focused suite (below) re-confirms every previously-cleared behavior
listed in the mission's Section 1 unchanged: secure source-file open,
`st_nlink == 1`, double/triple-fstat stability, bounded stability retry,
credential/ledger rotation rejection at source, final `built_data` immutable
attestation, final publication content observation, prior-destination stable
capture, baseline Git lifecycle semantics, SQLite source connection
attribution, SQLite WAL consistency, closed-world logical-ID/path grammar,
prefix/concatenated-ZIP rejection, public existing-archive receipt
semantics, malformed-verifier never-raises, test isolation, and duck
preservation. None of A2F8's own four BLOCKER fixes (B1–B4, the whole-archive-
level capture/verify/rollback/ZIP-prefix mechanisms) needed to change at all
this pass — every fix below operates one level down, at the *member*, not
the whole archive.

## B1 — Captured source bytes lost authority at mutable staging (BLOCKER)

**Finding.** Every physical member (a persona, `prefs.json`, a Project
document, a custom tool, …) was captured once via the stable-capture
boundary, but only the resulting **staged pathname** was kept on the
member's dict — the collector's own captured bytes were discarded
immediately after the file was written to `staging_root`.
`_build_agent_backup_core` then reopened that pathname by name for the
manifest hash/size (`_sha256_file(staged)`), and reopened it a **second**
time, independently, for the ZIP payload (`zipfile.write(staged_path)`).
A2R9 proved this live: substitute the staged file's content between those
two reopens — same-inode overwrite, truncate, append, atomic replacement, or
a symlink/hardlink replacement all worked identically — and the archive
contains the *substituted* bytes under a manifest hash computed from the
*original* bytes. The identical class of gap existed for SQLite members:
`_snapshot_sqlite` wrote the online-backup destination to `staged_path` and
returned nothing, so `_collect_database_members` separately reopened
`staged_path` for `_sqlite_metadata`, and `_build_agent_backup_core`
reopened it *again* for the manifest hash and *again* for the ZIP payload —
three independent reopens of one mutable file for one archived member.

**Root cause.** "Capture the bytes" and "remember where they were staged"
were treated as the same event. They are not: a captured-bytes value is
immutable Python data; a staged pathname is a mutable filesystem name any
same-UID process (including this one, by accident or by an adversarial
extension) can rewrite at any later moment. Every reopen-by-name after the
first capture was a fresh opportunity for that name to mean something else.

**Architectural repair.** The formerly-separate `_stage_physical_state_file`
(returned only a path) and `_capture_and_stage_physical_state_file` (the
one dual-use exception that also returned bytes) are merged into one
function that **always** returns `(data, staged_path)`. Every collector
(`_collect_preference_members`, `_collect_custom_tool_members`,
`_collect_audit_members`, `_collect_overlay_dir`, `_collect_project_members`,
`_collect_metadata_members`) now carries `data` — the immutable captured
bytes — directly on its member dict. `_build_agent_backup_core`'s manifest
hash/size (`hashlib.sha256(m["data"])`/`len(m["data"])`) and the ZIP payload
(`zf.writestr(ZipInfo(...), m["data"])`, never `zf.write(staged_path)`) are
both derived from that *same* `data`. `staged_path` still exists on the
dict — it is occasionally useful for diagnostics, and SQLite's online-backup
API still requires a real destination file — but it is transport/cache only
from this pass forward: nothing in `core/agent_backup.py` ever rereads it to
determine payload bytes, a hash, a size, or manifest/ZIP content.

SQLite members get the equivalent fix via a different, source-vetted
mechanism: `_snapshot_sqlite` now captures the exact checkpointed bytes via
`sqlite3.Connection.serialize()`, called on the **live**
`dest_conn` — the same connection that was just backed up into,
checkpointed (`PRAGMA wal_checkpoint(TRUNCATE)`), and integrity-checked —
**before** that connection is ever closed, and returns those bytes to the
caller. `_collect_database_members` validates and archives from that
returned `data` alone; `_sqlite_metadata_from_bytes` (new) inspects it via a
fresh in-memory `deserialize()` rather than reopening `staged_path` a second
time. Source-vetting this (not assuming API availability, per the mission's
own instruction) surfaced one genuine, non-obvious requirement: a
destination connection left in `wal` journal mode after
`wal_checkpoint(TRUNCATE)` — TRUNCATE empties the WAL file, it does not
reset the connection's own journal_mode pragma — serializes without error,
but the resulting bytes fail to `deserialize()` into a fresh in-memory
database afterward (`OperationalError: unable to open database file`,
because a WAL-mode header implies sidecar `-wal`/`-shm` files an in-memory
database has no real path to provide). Switching to `PRAGMA
journal_mode=DELETE` immediately before `serialize()` fixes this without
changing the checkpointed page content at all — verified empirically:
`serialize()`'s output is byte-identical to what is physically on disk at
`staged_path` the instant it is called, both before and after this fix, and
completely unaffected by a same-inode corruption of `staged_path` performed
immediately afterward. `_verify_sqlite_payload` (used both for this
pass's immediate build-time validation and unchanged at final-archive verify
time) was also switched from "write to a throwaway temp file, reopen it by
name" to the same in-memory `deserialize()` mechanism, for the same reason
Section 9 asks for it: eliminating a filesystem round-trip in the one
remaining path-based capture primitive this module still used internally.

**Regression tests** (`tests/test_agent_backup.py`):
`test_staged_persona_file_mutated_after_capture_does_not_affect_archive`,
`test_staged_persona_file_deleted_after_capture_does_not_affect_archive`,
`test_immutable_member_pipeline_survives_same_inode_atomic_replacement`,
`test_staged_sqlite_file_corrupted_after_capture_does_not_affect_archive`
(supersedes the now-obsolete `test_staged_db_corrupted_after_snapshot_
check_still_fails_the_build`, whose premise — that a post-capture staging
corruption *must* fail the build — was exactly the seam this pass closes;
the new test asserts the build succeeds and the archive is byte-identical
to the pre-corruption committed state, both directly and via
`verify_agent_backup`),
`test_flight_recorder_db_staged_file_corrupted_after_capture_does_not_
affect_archive` (the second database, per Section 10's explicit ask),
`test_snapshot_captured_data_matches_disk_before_post_capture_corruption`.

**Live adversarial replay (outside pytest).** Baseline dual-use member
(shipped_baseline_hashes.json, still archived *and* consulted for
provenance classification): captured, then staged-file mutated to a hostile
`{"format": "EVIL", ...}` payload — archive and provenance decisions both
reflect only the originally-captured bytes; full archive still strictly
verifies. SQLite **role-valid** substitution (a real, schema-correct
`lumina.db`-shaped decoy with a different row, `os.replace`d over
`staged_path` after `_snapshot_sqlite` returns): archive contains the
original `Test Chat` row, zero occurrences of the decoy's `EVIL SUBSTITUTE`
row, and still strictly verifies — proving the fix holds even when the
substitute is independently role-valid, not merely corrupt.

**Remaining limitation.** `staged_path` files still exist on disk for the
duration of a build (SQLite's backup API requires one; plain-file staging
keeps one for diagnostics) — they are inert, but they do consume the same
temporary disk space this module has always used. No change from prior
passes.

## B2 — Recovery artifacts were reported without content verification (BLOCKER)

**Finding.** `_preserve_rollback_failure_artifact` (the absolute last
resort, reached only when both a forward publish and its own rollback have
already failed) wrote the prior archive's immutable bytes to a fresh,
randomly-named file, fsynced it, and returned the bare path — without ever
proving the resulting path still held those exact bytes. A caller (and a
human reading the resulting error message) had no actual evidence the
"preserved" file was uncorrupted.

**Root cause.** "Wrote successfully" and "is verifiably present and
correct" were conflated. `fsync` proves bytes reached the storage layer at
the moment of the call; it says nothing about a same-inode mutation a
moment later, nor about whether the write itself somehow produced different
bytes than intended.

**Architectural repair.** `_preserve_rollback_failure_artifact` now: writes
to a fresh, exclusive, randomly-named `*.zip.part` temp file; fsyncs the
file; atomically finalizes it (`os.replace` to the final `*.zip` name, so no
reader can ever observe a partially-written artifact under its final name);
best-effort fsyncs the containing directory via the new
`_best_effort_fsync_dir` (returns whether it actually succeeded — Section 15's
"report the durability limitation truthfully" is satisfied by returning this
boolean rather than a bare, unconditional success claim); **re-observes**
the finalized artifact via the exact same universal stable-capture boundary
(`_open_and_capture_stable_bytes`) every other source in this module uses;
requires `SHA256(observed) == expected_sha256`; and strictly re-verifies the
observed bytes via `verify_agent_backup`. Only if every one of those checks
passes does the result carry `verified_at_preservation: True`. Any failure
along the way still reports whatever bytes made it to disk — by
construction, this function is only ever reached after both a forward
publish *and* a rollback have already failed, so whatever it manages to
preserve, however unverified, is the last remaining copy, and is therefore
kept rather than deleted — but labeled `verified_at_preservation: False`
with a `note` beginning `"UNVERIFIED recovery artifact..."` and the specific
reason. `_build_agent_backup_core`'s own rollback-failure branch was updated
to phrase its raised `AgentBackupError` message accordingly:
`"VERIFIED-preserved"` (with hash and directory-durability status) on a
genuine success, `"an UNVERIFIED recovery artifact was preserved"` on
anything less — never the old unconditional `"bytes are preserved... for
manual recovery"` wording regardless of whether that claim was actually
earned.

**Regression tests:**
`test_preserve_rollback_failure_artifact_reports_verified_on_clean_success`,
`test_preserve_rollback_failure_artifact_reports_unverified_on_post_
finalize_corruption` (corrupts the artifact at the `_best_effort_fsync_dir`
call site — the natural "after finalize, before re-observation" injection
point), `test_preserve_rollback_failure_artifact_reports_unverified_on_non_
archive_bytes`, `test_preserve_rollback_failure_artifact_reports_unverified_
on_hash_mismatch`, and `test_b3_rollback_failure_message_reports_verified_
preservation` (full end-to-end pipeline: a genuine forward-publish-then-
rollback failure now produces a message containing `"VERIFIED-preserved"`,
and the recovery file on disk is byte-identical to the prior good archive).
The pre-existing `test_b3_rollback_failure_preserves_recovery_artifact_
with_expected_hash` (A2F8) is unmodified and still passes unchanged.

**Live adversarial replay (outside pytest).** An atomic-replacement attack
injected at the directory-fsync call site (the same post-finalize,
pre-reobservation window the unit test above exercises, replayed via
`os.replace` against a decoy instead of an in-place corruption) still
produces `verified_at_preservation: False`, a non-`None` `path` that still
exists on disk, and a `note` containing `"UNVERIFIED"`.

**Remaining limitation.** `verified_at_preservation: True` describes the
artifact *at the moment this function returned* — it is not, and does not
claim to be, a promise that the mutable recovery path can never change
again afterward (the same honestly-time-bounded framing this module already
applies to `publication_observation`).

## B3 — Duplicate/non-standard JSON authority semantics were accepted (BLOCKER)

**Finding.** Unrestricted `json.loads(...)` applies last-key-wins semantics
to a repeated object key at any nesting depth, and parses the non-standard
`NaN`/`Infinity`/`-Infinity` numeric tokens as a Python-specific extension —
neither is acceptable for a format whose fields (`state_class`,
`restore_policy`, `rebind_policy`, `required`, `sha256`, `size`,
`backup_mode`, `format_version`, …) carry restore/rebind/executable
authority.

**Root cause.** The module never had its own JSON contract — it inherited
whatever Python's `json` module happens to tolerate, which is a superset of
interoperable JSON.

**Architectural repair.** One strict decoder, `_strict_json_loads`, used at
all three `json.loads` call sites in this module (`_parse_baseline_hashes`,
`_verify_prefs_payload`, `_verify_agent_backup_inner`'s manifest.json
parse). It passes `object_pairs_hook=_strict_object_pairs_hook` (raises
`_DuplicateJSONKeyError`, a `ValueError` subclass, on any repeated key —
`json.loads` invokes this hook once per JSON object it decodes, bottom-up,
so it applies uniformly to the root manifest object and every nested object
inside it with no separate wiring per nesting level) and
`parse_constant=_reject_non_finite_json_constant` (raises
`_NonFiniteJSONConstantError`, also a `ValueError` subclass, on
`NaN`/`Infinity`/`-Infinity`). Both are `ValueError` subclasses specifically
so every existing call site's exception handling (`except Exception`,
`except json.JSONDecodeError` widened to `except ValueError`) already
covers the new rejection paths without needing bespoke handling per site.

**Regression tests:** `test_strict_json_loads_rejects_duplicate_key_at_root`,
`test_strict_json_loads_rejects_duplicate_key_nested`,
`test_strict_json_loads_rejects_duplicate_key_in_bytes_input`,
`test_strict_json_loads_rejects_non_finite_constants` (parametrized over
all three tokens), `test_strict_json_loads_accepts_ordinary_well_formed_
json` (a genuine anchor test — the strict decoder must not reject valid
JSON), `test_verify_rejects_manifest_with_duplicate_root_level_key`,
`test_verify_rejects_manifest_with_duplicate_member_field_key`,
`test_verify_rejects_manifest_with_nan_numeric_constant`,
`test_baseline_hashes_with_duplicate_key_falls_back_to_unknown_not_crash`,
`test_prefs_payload_with_duplicate_key_rejected`.

**Live adversarial replay (outside pytest).** A real, genuine archive's
`manifest.json` text-surgically tampered to duplicate a member's
`restore_policy` key: rejected. The same manifest tampered to inject
`Infinity` into `format_version.minor`: rejected.

**Remaining limitation.** None identified — this is a closed, total fix for
every JSON document this module parses.

## B4 — Local ZIP headers were not fully bound to central-directory truth (BLOCKER)

**Finding.** Strict verification already agreed local and central records
on filename, compression method, and compressed size (A2F7/A2F8), and
already rejected a streaming data-descriptor bit specifically, but never
compared CRC-32, uncompressed size, or the full general-purpose flags word
— a local header could disagree with the central directory on any of those
three fields and still verify `valid=True`.

**Root cause.** The original B4 fix (A2F8) was scoped to closing the
leading-prefix/concatenated-archive attack (physical layout tiling) and the
narrowest possible per-member agreement needed to prove that; it was never
extended to the full set of fields the builder writes authoritatively per
member.

**Architectural repair.** `_parse_local_header` now also extracts and
returns the local header's own CRC-32 field (previously parsed and
discarded as `_crc32`). `_validate_zip_physical_structure` now additionally
requires, per member: `local["crc32"] == info.CRC`, `local["uncomp_size"]
== info.file_size`, and `local["flags"] == info.flag_bits` (full equality,
not merely the single data-descriptor bit already checked). Section 24's
correction is explicit in the new check's own comment: A2R9 independently
confirmed a genuinely Unicode filename member legitimately carries
`flag_bits & 0x800` — there is no single fixed `flag_bits` value every
builder-emitted member shares, so the contract is **equality** between
local and central for whatever each member's own records actually declare,
never a hardcoded expected constant.

**Regression tests:** `test_b4_local_central_crc_mismatch_rejected`,
`test_b4_local_central_uncompressed_size_mismatch_rejected`,
`test_b4_local_central_flags_mismatch_rejected`,
`test_b4_unicode_member_name_verifies_with_utf8_flag_set` (a genuinely
non-ASCII persona filename — `lümina☃.json` — builds, archives, and
strictly verifies, with `flag_bits & 0x800` confirmed set on the real
`ZipInfo`, proving the new equality check accepts the actual subset the
builder emits rather than assuming a constant).

**Remaining limitation.** None identified for Lumina's own emitted subset;
this check remains deliberately scoped to the narrow structural language
`zipfile.ZipFile(..., ZIP_DEFLATED)` actually produces, not general ZIP
creativity (unchanged design principle from A2F8).

## H1 — Builder could emit ZIP64 that strict verifier rejects (HIGH, release-gating)

**Finding.** The builder's `zipfile.ZipFile(tmp_path, "w",
zipfile.ZIP_DEFLATED)` call left `allowZip64` at the library default
(`True`), while the strict verifier has never accepted ZIP64
(`_ZIP64_SENTINEL_32`/`_ZIP64_SENTINEL_16` checks, unchanged since
A2R7/A2F7) — meaning the builder could silently create an archive (e.g.
more than 65,535 members) that this same module's own verifier would then
reject, an internally inconsistent format contract.

**Root cause.** The verifier's non-ZIP64 policy was never mirrored onto the
builder's own construction call.

**Architectural repair.** The builder now passes `allowZip64=False`
explicitly. The `with zipfile.ZipFile(...)` block is wrapped in a `try`/
`except zipfile.LargeZipFile`, converting it into an honest
`AgentBackupError` (`"this backup would require ZIP64 extensions... Agent
Backup v1 is intentionally non-ZIP64..."`) raised **before** any
publication is attempted — the pre-existing `finally: os.remove(tmp_path)`
cleanup already guarantees no partial temp file survives regardless of
where inside the `with` block the exception originates (source-vetted:
`LargeZipFile` for an excessive file count is raised from `ZipFile.close()`,
i.e. from `__exit__`, at which point the temp file may already contain
local headers with no central directory — the existing cleanup handles this
correctly either way, unchanged).

**Regression test:**
`test_builder_refuses_to_emit_zip64_archive_before_publication` — patches
`zipfile.ZIP_FILECOUNT_LIMIT` down to a trivially small value (source-vetted:
this is a live, effective module global `zipfile`'s own internal file-count
check reads at call time, confirmed empirically) so a genuinely small, real
`_build()` call exercises the *actual* `zipfile.LargeZipFile` path a real
65,536+-member archive would take — a safe, fast (well under a second)
integration exercise of real builder behavior per Section 29's "safe
integration fixture" option, not a hand-simulated stand-in — and confirms
`AgentBackupError` mentioning `"ZIP64"`, no destination file, and no
leftover `.agent_backup_*` temp files. `test_strict_verifier_rejects_hand_
built_zip64_eocd` gives the other half of the contract (previously
implemented but untested directly) explicit coverage: a hand-crafted EOCD
declaring the ZIP64 sentinel values is still rejected.

Independently, outside pytest, the literal 65,536-zero-byte-entry
integration scenario A2R9 itself used was reproduced directly against raw
`zipfile.ZipFile(..., allowZip64=False)` (not routed through the full
collection pipeline, which has no code path to produce that many members
without the fixture itself constructing 65,536 fake overlay files): confirmed
`zipfile.LargeZipFile: Files count would require ZIP64 extensions` raised
in ~2.3 seconds, proving the underlying stdlib behavior this pass now relies
on is real, not assumed.

**Remaining limitation.** None — v1 is now non-ZIP64 by construction on
both the builder and verifier side; no undocumented magic values were
introduced (the existing `_ZIP64_SENTINEL_32`/`_ZIP64_SENTINEL_16`/
`zipfile.ZIP64_LIMIT`/`zipfile.ZIP_FILECOUNT_LIMIT` constants are all
stdlib-defined, not reinvented).

## Sparse/large-file follow-up (Section 35)

`AGENT-BACKUP-SPARSE-LARGEFILE-01` remains open and nonblocking, unchanged.
The immutable-member refactor does increase resident memory somewhat: every
physical member's bytes (previously only ever touched via a lazy, one-file-
at-a-time reopen at hash time and again at zip-write time) are now held as
a Python `bytes` object on that member's dict for the span between
collection and the ZIP write completing. For Lumina's real data shapes
(personas/skills/tool-profiles are single-digit KB text; avatars/voices are
typically low-single-digit-MB images/audio; `lumina.db`/`flight_recorder.db`
are the largest members and were already fully read into memory at hash
time even before this pass, since `_sha256_file` streams from disk but the
prior `zipfile.write()` call itself reads the whole file into its own
internal buffer during compression) the net *additional* memory this pass
introduces is approximately one extra full-size copy of the non-database
members held concurrently (a few, at most low tens of, MB for a typical
installation) — not a categorically new order of magnitude, and not
measured against multi-gigabyte test data per the mission's own guidance
not to solve performance here.

## Duck (Section 37)

`skills/rubber-duck-debugging.md`: present, `sha256sum` confirms
`d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be` exactly
matching the required hash, captured by the fixture-based tests exactly as
before. `git ls-files` confirms it is not tracked in git at all;
`git check-ignore -v` confirms `.gitignore:39` matches it explicitly — not
shipped baseline, provenance unknown, exactly the historical record.

## Self-adversarial replay, outside pytest (Section 38)

11/11 checks passed (script: session scratchpad, not committed): baseline
dual-use member staged-file mutation (archive/provenance both reflect the
original capture); SQLite role-valid substitution for `lumina.db` (archive
contains the original row, zero decoy rows, still strictly verifies);
recovery-artifact atomic-replacement attack at the directory-fsync
injection point (reported UNVERIFIED, artifact kept, never claimed
verified); duplicate `restore_policy` member-field key in a real archive's
own manifest (rejected); `Infinity` numeric constant in a real archive's
own manifest (rejected). See the B1–H1 sections above for the additional,
narrower live replays specific to each finding.

## Focused suite (Section 39)

```
tests/test_agent_backup.py
collected: 322
passed:    322
failed:    0
warnings:  1 (pre-existing, unrelated: a UserWarning from zipfile itself
              inside test_verify_rejects_duplicate_zip_member_name, which
              deliberately constructs a duplicate-member-name archive to
              test the verifier's own rejection of it)
runtime:   ~33s
native exit: 0
```

294 A2F8 tests + 28 net-new A2F9 tests (1 sqlite-corruption-immunity anchor
replacing the now-obsolete pre-A2F9 test, 27 additional new regressions) =
322, verified by direct pytest collection count, not inherited arithmetic.

## Complete release suite (Section 40)

Ran naturally to completion — no stall this time, including through
`tests/test_review_panel.py` (32/32, clean), which is where A2R9's own
attempt was left stuck. This confirms the A2R9 stall was the
previously-known, pre-existing, intermittent `test_review_panel.py`/
`test_coding_checkpoint_tools.py`-family deadlock (independently
documented before this pass) rather than anything caused by this pass's
own changes.

```
collected: 3885
passed:    3885
failed:    0
warnings:  1 (the same pre-existing, unrelated zipfile UserWarning noted
              in the focused-suite section above)
skips/xfailed: 0
runtime:   436.19s (0:07:16)
native exit: 0
```

Arithmetic checked directly, not inferred: 3857 (A2F8's full-suite total)
− 294 (A2F8's own `test_agent_backup.py` count, included in that 3857) +
322 (this pass's `test_agent_backup.py` count) = 3885, exactly matching
the number pytest itself collected and passed. The one PID confirmed and
gracefully terminated in Section 0 was the *previous* (A2R9) attempt's
already-stalled process; this pass's own full-suite run is a fresh
invocation, not a resumption of that one.

## The fourteen load-bearing architectural statements (Sections 34/41, cumulative)

Statements 1–8 (A2F7/A2F8) are restated verbatim in `core/agent_backup.py`'s
own module docstring and remain unchanged and independently re-verified by
this pass's full focused-suite pass. Six further statements this pass adds:

9. Once source bytes are securely captured (a physical file via
   `_stage_physical_state_file`, a SQLite snapshot via `_snapshot_sqlite`'s
   own `serialize()` capture), no mutable staging pathname may redefine
   that member's content, hash, size, or ZIP payload ever again.
10. SQLite role/schema/`quick_check` validation applies to the exact
    immutable snapshot bytes ultimately archived — proven via a fresh
    in-memory `deserialize()` of those same bytes, never a separate reopen
    of a staging pathname.
11. Recovery artifacts are only ever reported verified after content
    recapture, hash comparison, strict re-verification, AND durability
    observation — never merely "written and fsynced."
12. Manifest JSON (and every other authority-bearing JSON document this
    module parses) rejects a duplicate key at any nesting depth and any
    non-finite numeric constant.
13. Strict ZIP verification agrees local and central directory records on
    every field the builder writes authoritatively — filename,
    general-purpose flags, compression method, CRC-32, compressed size,
    and uncompressed size — for the actual builder-emitted subset, not a
    hardcoded expectation.
14. Agent Backup v1's builder and verifier share one explicit ZIP64
    contract (non-ZIP64, on both sides) — the builder must never be able
    to silently emit a structure the same build's own verifier would
    reject.

## STOP

No commit. No push. No mirror. No cleanup beyond the one specifically
verified stale A2R9 pytest process (Section 0). Candidate returned for
AGENT-BACKUP-RESTORE-A2R10.
