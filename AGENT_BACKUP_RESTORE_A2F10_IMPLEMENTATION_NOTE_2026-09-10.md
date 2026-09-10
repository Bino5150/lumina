# AGENT-BACKUP-RESTORE-A2F10 — Implementation Note

Tenth repair pass. AGENT-BACKUP-RESTORE-A2R10 independently re-attacked
the A2F9 candidate and found the architecture itself finally held — the
immutable member pipeline, SQLite `serialize()`-based snapshot, baseline
provenance, strict JSON, rollback/recovery, and publication-content
observation all survived a full 48-case staging-substitution attack
matrix — but found three narrower, still release-gating findings, all at
the "format closure / truthful durability / resource bound" layer rather
than the architecture itself:

```text
B1  Strict verifier accepts ZIP structures the v1 builder cannot emit.
H1  Main/rollback publication hides parent-directory durability failure.
H2  Backup has ~5x measured peak-memory amplification with no aggregate bound.
```

This note records root cause, architectural repair, regression coverage,
live adversarial replay, and remaining limitation for each finding, then
the focused/full suite results.

## Repository and process gate (Section 0)

- Release (`~/lumina-release`, `main`): HEAD `ee40259c980d3c6fcca56fc2f27dcefd3814c178`,
  identical to `origin/main` (0 ahead / 0 behind), working tree clean
  apart from this campaign's own untracked deliverables
  (`AGENT_BACKUP_RESTORE_*` notes, `core/agent_backup.py`,
  `tests/test_agent_backup.py`, `metadata/`) — matches the A2R10 baseline
  exactly (verified fresh, not inherited). No in-progress git operation
  (no `MERGE_HEAD`/`REBASE_HEAD`/`CHERRY_PICK_HEAD`/rebase-merge/
  rebase-apply state). `push.default` unset; no `pre-push` hook in this
  repo; `origin` push URL is the ordinary `https://github.com/Bino5150/lumina.git`.
- Dev (`~/lumina`, `dev-local`): HEAD `9b09542489210445b6b7fcc4ef2148e5f9eb64a8`,
  matches the A2R10 baseline exactly; release HEAD `ee40259` confirmed an
  ancestor of dev HEAD (dev's own HEAD is literally "Merge release: README
  and persona assets", i.e. the merge commit of that exact release
  commit). `release-local` remote push URL is the literal string
  `DISABLED`; a `pre-push` hook blocks pushes from this repo
  unconditionally. Dev's own working tree carries one modified generated
  file (`projects/lumina-dev/codebase.md`, expected drift) and four
  untracked `skills/*.md` files from an unrelated in-progress thread —
  routine untracked storage, left untouched by this campaign, per
  established convention.
- A2R10's own adversarial fixtures and one explicitly-requested live
  backup under `/tmp` were left in place, per this campaign's explicit
  instruction not to delete them without separate authorization; nothing
  under `/tmp` was touched by this pass.

## Preserved A2R10-cleared architecture (Section 2)

Not redesigned, not touched beyond what B1/H1/H2 themselves required:
the immutable-member capture pipeline (`_stage_physical_state_file`/
`_snapshot_sqlite` returning `(data, staged_path)`, never reread by
name), SQLite exact-byte serialization/validation, baseline single-
snapshot git-anchored provenance, strict JSON (duplicate-key/non-finite
rejection), verified rollback/recovery (`_publish_immutable_bytes`/
`_preserve_rollback_failure_artifact`), and publication content
observation. No filesystem staging was put back into the authority
chain. The full pre-existing focused suite (see Section 39) confirms all
of this remains green.

## B1 — Strict verifier accepted ZIP structures the v1 builder cannot emit (BLOCKER)

**Finding.** Local/central AGREEMENT (A2F8/A2F9's own B4 closure) is not
the same claim as "the v1 builder could have produced this." Both
records could agree on `ZIP_STORED`, a reserved/encryption/DEFLATE-
option flag bit, or a non-empty extra field — none of which this
module's own `zipfile.ZipFile(..., ZIP_DEFLATED)` builder ever writes.
Separately, the builder's physical-entry-count ceiling was enforced
nowhere before construction: at exactly 65,535 physical entries the
builder itself still succeeds (`zipfile.ZIP_FILECOUNT_LIMIT`'s own check
is `> 65535`, not `>= 65535`), yet that exact count is physically
indistinguishable from the ZIP64 "read the real count elsewhere" sentinel
value, and the strict verifier — correctly — refuses it either way. A
genuine v1 archive could therefore be built, fully paid for in
collection/compression time and temp-directory space, and only THEN
discovered to be self-contradictory at its own verify step.

**Root cause.** The v1 ZIP "language" (Section 3's framing) was never
written down as a closed, positive set of what the builder can produce —
only as a series of local/central equality checks, which prove internal
consistency but not builder-plausibility. The entry-count ceiling
existed only on the verifier side (the pre-existing ZIP64-sentinel
check), never mirrored onto the builder's own construction path.

**Source-vetting (Section 4).** `source_vet_zip.py` (session scratchpad,
not committed) built a real archive via `core.agent_backup.build_agent_backup`
against a realistic env — ASCII and genuinely non-ASCII (`lümina☃.json`)
persona filenames, a zero-byte voice file, binary avatar content, a
highly-incompressible skill file (`os.urandom`), and both SQLite
databases (one with a 50,000-byte row to exercise a larger compressible
payload) — then inspected every physical member's LOCAL and CENTRAL
records directly (not zipfile's decoded convenience view alone). Every
single member, including `manifest.json` itself, came back:

```text
compression method:   8 (ZIP_DEFLATED) -- no exceptions, including the
                       zero-byte and highly-incompressible members
general-purpose flags: 0x0000, or exactly 0x0800 for the one genuinely
                       non-ASCII filename -- no other bit ever set
local/central extra field length: 0, for every member, no exceptions
create/extract version: 20 (2.0), uniformly
```

This matches the builder's own source directly:
`_build_agent_backup_core`'s ZIP-writing loop unconditionally sets
`zinfo.compress_type = zipfile.ZIP_DEFLATED` and never touches
`zinfo.extra`, for every member and for `manifest.json` alike — there is
no code path in this module that could emit anything else.

The 65,535-entry boundary was independently reproduced directly against
raw `zipfile.ZipFile(..., allowZip64=False)`:

```text
n=65533: build succeeds, physical-structure check: []
n=65534: build succeeds, physical-structure check: []
n=65535: build succeeds, physical-structure check: ["archive requires
         ZIP64 extensions..."] -- ALREADY correctly rejected by the
         pre-existing total_entries==0xFFFF sentinel check, but only
         AFTER a full, wasted 65,535-member build (~1.3s for zero-byte
         members; real state would cost much more)
n=65536: zipfile.LargeZipFile raised during build itself
```

**Architectural repair.**

1. `_ALLOWED_COMPRESSION_METHODS = frozenset({zipfile.ZIP_DEFLATED})` and
   `_ALLOWED_GENERAL_PURPOSE_FLAGS = frozenset({0x0000, 0x0800})`,
   source-vetted as above. `_validate_zip_physical_structure` now checks
   membership in these sets for BOTH the central-directory record and
   the local header, INDEPENDENTLY of (in addition to, not instead of)
   the pre-existing local==central equality checks — so two mutually-
   agreeing but builder-impossible records are rejected either way, not
   only a local/central disagreement.
2. `_parse_local_header` now also returns `extra_len` (previously parsed
   and folded into `header_size` only, never checked in its own right).
   `_validate_zip_physical_structure` requires `info.extra` (central) and
   `local["extra_len"]` (local) to both be zero-length, checked
   independently of each other — covering "unknown extra", "fake ZIP64
   extra" (0x0001), "local-only extra", and "central-only extra" all via
   the SAME blanket check (it never inspects an extra field's content or
   ID, only its length), rather than four separate special cases.
3. `_MAX_PHYSICAL_ZIP_ENTRIES = 65534` (one below the 0xFFFF ZIP64-
   sentinel boundary). `_build_agent_backup_core` now computes
   `total_physical_entries = len(members) + 1` (the `+1` being
   `manifest.json`, the one always-generated file today — computed
   dynamically, NOT a separately hardcoded "65533" constant, per the
   mission's own explicit warning against a "brittle magic subtraction")
   immediately after collection completes, and raises a clear
   `AgentBackupError` naming the exact predicted count BEFORE ever
   opening a `zipfile.ZipFile` for writing — closing the "wasted full
   build, only then discovered invalid" gap.

**Regression tests** (`tests/test_agent_backup.py`):
`test_builder_output_matches_source_vetted_v1_zip_language` (integration
anchor, ASCII+Unicode+zero-byte+binary+both SQLite databases),
`test_verify_rejects_zip_stored_even_when_local_and_central_agree`,
`test_verify_rejects_reserved_flag_bits_even_when_local_and_central_agree`
(parametrized over encryption/DEFLATE-option/patched-data/strong-
encryption/a reserved bit), `test_verify_rejects_nonzero_extra_field_even_when_local_and_central_agree`
(a real, well-formed, LOCAL/CENTRAL-CONSISTENT extra field built via
`zipfile.ZipInfo.extra`, covering unknown-ID and fake-ZIP64-ID extras
structurally), `test_verify_rejects_local_only_extra_field` (raw byte
surgery on the archive's own LAST local record, so only the trailing
central directory + EOCD need offset correction, never another member's
own local record), `test_max_physical_zip_entries_below_zip64_sentinel_boundary`,
`test_builder_refuses_when_physical_entry_count_would_reach_v1_ceiling`
(fast, real `_build()` call against a patched-down ceiling),
`test_real_65534_physical_entries_builds_and_strictly_verifies`,
`test_real_65535_physical_entries_builds_but_verifier_rejects_as_ambiguous`,
`test_real_65536_physical_entries_raises_large_zip_file` (all three
exercised at REAL scale against raw `zipfile`, not hand-simulated),
`test_builder_corpus_every_variant_strict_and_receipt_verifies` (Section
42's builder corpus: ASCII/Unicode/zero-byte/binary/both SQLite DBs/
highly-compressible/incompressible/50+ members, every one strict-AND-
receipt-verifying).

**Live adversarial replay (outside pytest).** The 65,533/65,534/65,535/
65,536-entry boundary was reproduced directly against raw `zipfile`
(Section 4/9's own source-vetting above) rather than assumed from A2R10's
report. A genuinely Unicode persona filename (`lümina☃.json`) was
confirmed, live, to carry exactly `flag_bits=0x0800`, `extra_len=0`, and
`compress_type=8` in BOTH its local and central records.

**Remaining limitation.** The closed language does not constrain
`create_version`/`extract_version`/`internal_attr`/`external_attr`/
`disk_number_start` — the mission's own Section 3-7 enumerates
compression, general-purpose flags, extra fields, entry count, ZIP64,
and physical layout specifically; these version/attribute fields were
source-vetted (uniformly `20`/`0o644<<16`) but deliberately left
unconstrained rather than expanding the closure beyond what was asked,
per the "fix the gap found, don't rebuild the world" discipline. If a
future finding shows these fields are also exploitable, that is new,
separate evidence, not something this pass silently skipped.

## H1 — Publication durability was hidden, not merely best-effort (HIGH)

**Finding.** `_publish_immutable_bytes` already performed a best-effort
directory fsync after every publish (main or rollback), but its outcome
(success or `OSError`) was caught and unconditionally discarded — neither
the function's own return value, the rollback code path's narrated
error message, nor either public build entry point
(`build_agent_backup`/`build_agent_backup_with_receipt`) ever revealed
whether that fsync actually happened. Content-publication success and
directory-durability confirmation were conflated into one silent "best
effort, who knows" outcome.

**Root cause.** "The fsync was attempted" and "the fsync's result is
something a caller can see" were never actually the same thing — the
`try/except OSError: pass` swallowed the boolean outcome entirely rather
than surfacing it, even though `_best_effort_fsync_dir` (already used
elsewhere, for the recovery-artifact path) existed as exactly the right
primitive to reuse.

**Architectural repair.** Per the mission's own explicit instruction
(Section 14), A1's historical best-effort durability model is NOT
tightened into a new destructive "fsync failed therefore corrupt" rule —
instead, the two facts are separated and both reported truthfully:

1. `_publish_immutable_bytes` now calls the existing
   `_best_effort_fsync_dir(dest_dir)` helper (instead of an inline,
   result-discarding `try/except`) and includes `"directory_fsynced":
   bool` in its returned dict. A directory-fsync failure never turns an
   otherwise-successful, content-verified publish into a raised error.
2. The rollback success path's own narrated `rollback_note` now reads
   `"prior destination restored (content independently re-observed and
   confirmed by hash; directory durability confirmed)"` or `"...
   directory durability best-effort only, not confirmed)"` depending on
   the ACTUAL outcome of that specific rollback's own directory fsync —
   never a bare `"prior destination restored"` that elides the question
   entirely. Content restoration and durability confirmation are stated
   as two separate facts, exactly as Section 17 requires: never "rollback
   failed" when content restoration genuinely succeeded, never "durably
   restored" when the directory fsync did not.
3. `build_agent_backup_with_receipt`'s `publication` key already forwards
   the entire `publication_observation` dict verbatim, so it now
   automatically carries `directory_fsynced` too — no code change needed
   there beyond the field's addition upstream.
4. `build_agent_backup` (the plain, receipt-less public entry point) has
   no receipt to carry this fact through, and its return shape (the
   manifest dict actually written into the archive) is a pre-existing
   public contract this pass does not change. A directory-fsync failure
   is instead surfaced via ONE honestly-labeled warning APPENDED TO A
   SHALLOW COPY of the manifest's own `warnings` list — the immutable
   `_BuiltArchiveResult.manifest` object itself is never mutated (Section
   4's own architectural principle, preserved: "one immutable captured
   object is the source of every field"). The archive's own on-disk
   `manifest.json` content is unaffected either way (that content was
   necessarily finalized before publication and its own directory-fsync
   outcome could ever be known) — this is a nuance of the returned
   Python object, not a claim that the two now disagree in some load-
   bearing way.
5. Recovery-artifact handling (`_preserve_rollback_failure_artifact`)
   already modeled this distinction correctly since A2F9 and was left
   unchanged — this pass reuses, rather than reinvents, that semantic
   model, per Section 18's explicit instruction to avoid three different
   meanings of "fsync" across publication/rollback/recovery.

**Regression tests:**
`test_publish_immutable_bytes_reports_directory_fsynced_true_on_real_success`,
`test_publish_immutable_bytes_reports_directory_fsynced_false_without_failing_publication`,
`test_build_agent_backup_manifest_warns_when_directory_fsync_fails`,
`test_build_agent_backup_without_directory_fsync_failure_carries_no_spurious_warning`,
`test_build_agent_backup_with_receipt_publication_reports_directory_fsynced_false`,
`test_build_agent_backup_with_receipt_publication_reports_directory_fsynced_true`,
`test_rollback_success_reports_directory_durability_confirmed`,
`test_rollback_success_reports_directory_durability_best_effort_when_fsync_fails`
(both via the same `os.replace`-sabotage technique the pre-existing B3
rollback tests use, forcing a genuine forward-publish-content-mismatch
that drives `_build_agent_backup_core` into its real rollback path),
`test_replacement_backup_reports_directory_fsync_failure_same_as_first_backup`
(Section 19's explicit "replacement backup" case, distinct from a
first-ever publish).

**Live adversarial replay (outside pytest).** Confirmed directly (not
merely via the unit tests above) that monkeypatching `_best_effort_fsync_dir`
to always return `False` during an ordinary, otherwise-unsabotaged
`build_agent_backup` call still produces a byte-perfect, strictly-
verifying archive at `dest_path` — the directory-fsync outcome is
genuinely orthogonal to content correctness, exactly as Section 14
requires it to be.

**Remaining limitation.** None identified for the three publication
contexts this pass touches (main/replacement publication, rollback).
Recovery-artifact preservation already had this correctly, unchanged.

## H2 — No aggregate resource bound on either builder or verifier (HIGH)

**Finding.** AGENT-BACKUP-RESTORE-A2R10 measured near-linear ~5x peak-RSS
amplification over raw payload size (32 MiB payload → +159.8 MiB RSS
≈4.99x; 64 MiB → +319.7 MiB RSS ≈5.00x; a 32 MiB prior backup plus a
32 MiB replacement build → ~223.8 MiB added peak RSS) with NO aggregate
ceiling anywhere: not during collection (every member's captured bytes
simply accumulate, unbounded, across a build — the direct, necessary
cost of A2F9's own B1 fix, which correctly keeps every member's
immutable captured `data` resident rather than reopening a mutable
staging path), and not during verification of an existing/hostile
archive (a 72,530-byte archive decompressing a single 64 MiB member,
≈925:1 compression, added ~60 MiB RSS during ordinary receipt
verification, with nothing to stop a far larger one).

**Root cause.** Two related but genuinely separate gaps: (1) nothing
tracked a running total of captured-but-not-yet-published bytes during
collection, so an unusually large or numerous set of ordinary members
(or SQLite databases) could grow this process's resident memory without
limit; (2) `verify_agent_backup`/`build_backup_receipt` decompressed
every declared member via `zf.testzip()`/`zf.read()` with zero size
checking beforehand, trusting central-directory metadata is
authoritative for correctness (it is) without ALSO treating it as a
budget gate before decompression.

**Source-vetting the default (Section 21).** This machine's real, actual
`DATA_DIR`+`BASE_DIR` payload — everything this module archives —
totals under 20 MiB today (`lumina.db` 3.7 MiB, `flight_recorder.db`
4.0 MiB, `assets/` 5.6 MiB, everything else single-digit KB). This
codebase already has two existing size-ceiling conventions in the same
rough band: `core/flight_recorder.py`'s own `DEFAULT_MAX_DB_BYTES` (500
MiB) and `tools/sandbox.py`'s `MAX_MEMORY_BYTES` (512 MiB, a sandboxed
process's own address-space cap). **2 GiB** was chosen as the v1
aggregate uncompressed-payload default: comfortably above both existing
conventions and ~100x above real current usage, generous headroom for
genuine growth, while remaining a firm, finite ceiling rather than
"however much RAM happens to be free."

**Architectural repair.**

1. `_AggregateBudget` (`limit_bytes`, `consumed_bytes`, `charge(nbytes,
   label)`) — charged incrementally, INSIDE `_stage_physical_state_file`
   and `_snapshot_sqlite` themselves (the two universal capture
   boundaries every collector already funnels through), the INSTANT
   captured bytes exist, never after collection has already finished.
   Threading `budget` through every `_collect_*` function required
   touching each one's signature, but the actual charging logic lives in
   exactly two functions — matching this module's own "one capture
   boundary" architecture rather than inventing a parallel bookkeeping
   path. SQLite snapshots are charged identically to plain files (no
   database exemption, Section 23).
2. `_determine_aggregate_payload_budget(prior_backup_bytes)`: starts from
   `_resolve_configured_payload_budget()` (the 2 GiB default, or a
   bounded `LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES` environment override
   — following this codebase's existing `LUMINA_*` convention, validated
   against a documented `[16 MiB, 16 GiB]` range, silently ignored and
   falling back to the default outside it, never a partially-applied or
   arbitrary unvalidated integer), then — only if `/proc/meminfo`'s
   `MemAvailable` can be read at all (Linux-only, best-effort, never a
   new dependency) — clamps that ceiling DOWNWARD (never upward) so a
   conservative peak-memory estimate (`configured budget × 6` — above
   A2R10's own measured ~5.00x — `+ prior_backup_bytes + 64 MiB fixed
   overhead`) never exceeds half of currently-available memory. Host-
   memory discovery failing entirely (non-Linux, unreadable
   `/proc/meminfo`) leaves the configured/default ceiling untouched, per
   Section 27's explicit instruction. `prior_backup_bytes` is a cheap
   `os.stat()` (never a read) of any existing `dest_path`, taken before
   collection begins — Section 24's "rollback authority may hold a large
   prior backup resident too" is accounted for by shrinking the budget
   available to the NEW build's own members, not by ignoring it.
3. `_build_agent_backup_core`'s preflight (right after `_collect_members`
   returns, alongside the entry-count check) — no additional gate needed
   here beyond the incremental per-member charges already having run
   during collection itself.
4. `_verify_agent_backup_inner` now computes `infolist` unconditionally
   (previously gated behind `raw_bytes is not None`, even though
   `infolist()` needs no raw bytes at all) and checks EVERY member's
   TRUSTED, un-decompressed central-directory-declared `file_size`
   (never a hostile manifest's own self-reported "size" field, per
   Section 30) — both per-member and in aggregate — against
   `_resolve_configured_payload_budget()` BEFORE `zf.testzip()` or any
   `zf.read()` can materialize a single decompressed byte. Absolute
   limits, not a compression-ratio heuristic (Section 31): a
   legitimately highly-compressible member is never penalized purely for
   compressing well.
5. **Self-check, caught during this pass's own adversarial review**: the
   verify-side gate deliberately calls `_resolve_configured_payload_budget()`
   (the OPERATOR-configured ceiling) rather than referencing the bare
   `_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES` constant directly. The
   build-time effective budget is only ever clamped DOWNWARD from that
   same configured ceiling (never upward, Section 27) — so anything that
   survives build-time incremental charging is guaranteed to also survive
   this verify-time check, including a build's OWN immediate self-
   verification of the archive it just produced
   (`_capture_and_verify_archive`, called before publication). Reading
   the bare default constant directly at verify time instead would have
   silently reintroduced exactly the "two independently-tunable numbers
   that could diverge" failure mode this pass exists to close, for any
   operator who raises the ceiling via the environment override — caught
   and fixed before this candidate was returned, with a dedicated
   regression test (`test_build_with_raised_configured_budget_does_not_fail_its_own_self_verification`).

**Regression tests:**
`test_aggregate_budget_charges_incrementally_and_rejects_over_limit`,
`test_aggregate_budget_exact_boundary_at_limit_succeeds`,
`test_aggregate_budget_one_byte_over_limit_fails`,
`test_determine_aggregate_payload_budget_uses_default_when_meminfo_unavailable`,
`test_determine_aggregate_payload_budget_clamps_downward_for_low_available_memory`,
`test_determine_aggregate_payload_budget_never_exceeds_configured_default`,
`test_determine_aggregate_payload_budget_accounts_for_prior_backup_bytes`,
`test_resolve_configured_payload_budget_env_override_respected`,
`test_resolve_configured_payload_budget_env_override_ignored_when_invalid`
(parametrized: non-integer, zero, negative, and above-ceiling),
`test_build_fails_when_aggregate_payload_exceeds_configured_budget`,
`test_build_fails_partway_through_many_small_members_summing_over_budget`
(30 members, none individually near the limit, whose SUM exceeds it),
`test_build_fails_on_single_oversized_member` (the complementary case —
one member alone, as distinct from many small ones),
`test_large_sqlite_snapshot_charged_against_same_budget` (no database
exemption), `test_build_succeeds_comfortably_under_budget` (anchor),
`test_verify_rejects_single_member_declaring_oversized_uncompressed_size`
(raw central+local size-field surgery — a real, legitimately-sized
payload with an attacker-lied-about declared size, the exact
compression-bomb shape, without needing gigabytes of real test data),
`test_verify_rejects_aggregate_declared_size_over_budget_with_no_single_member_over`
(many legitimately-sized real members whose sum exceeds a patched-down
budget, none individually over), `test_verify_accepts_real_archive_comfortably_under_decompression_budget`,
`test_build_backup_receipt_also_honors_decompression_budget` (Section 29
names this function explicitly), `test_build_with_raised_configured_budget_does_not_fail_its_own_self_verification`.

**Live adversarial replay / memory measurement (outside pytest, Section
34).** Fresh, isolated child-process peak-RSS measurements
(`resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`, one dedicated
subprocess per measurement, matching A2R10's own methodology —
`measure_rss.py`/`rss_child.py`, session scratchpad, not committed):

```text
baseline (no extra payload):        32.4 MiB peak RSS
32 MiB payload:                    192.4 MiB peak RSS  (+160.0 MiB, ~5.00x)
64 MiB payload:                    352.4 MiB peak RSS  (+320.0 MiB, ~5.00x)
32 MiB prior + 32 MiB replacement: 224.4 MiB peak RSS  (total; A2R10's own
                                    independently-measured figure for this
                                    exact scenario was ~223.8 MiB -- this
                                    pass's fresh measurement reproduces it
                                    almost exactly)
```

This independently reproduces A2R10's own reported ~5x amplification
(+159.8 MiB/+319.7 MiB at 32/64 MiB) to within measurement noise — the
phenomenon is real, not a one-off. Current effective budget on this
machine, measured fresh (varies with host memory pressure at
measurement time, by design — Section 27):

```text
MemAvailable right now:               6553.2 MiB
configured/default budget:            2048.0 MiB (2 GiB)
effective (host-clamped) budget now:   535.4 MiB
estimated worst-case peak working set: budget x safety_factor(6) + 64 MiB
                                       overhead =~ 3276.6 MiB
```

We do not need A2F10 to eliminate the ~5x amplification — Section 35
below keeps that as explicitly future work — we need it bounded and
explicit, which it now is: on THIS host, right now, Agent Backup will
refuse to collect more than ~535 MiB of aggregate uncompressed payload
(clamped down from the 2 GiB default because current available memory is
itself limited), and the worst-case peak resident memory a build at that
ceiling could produce is conservatively estimated at ~3.3 GiB — a
concrete, finite number instead of "however much RAM happens to be
free."

**Remaining limitation.** This pass does not eliminate the ~5x
amplification (that is `AGENT-BACKUP-SPARSE-LARGEFILE-01`, explicitly
kept as future, nonblocking performance work per Section 35) — it makes
that amplification bounded and explicit. On a host where currently-
available memory is extremely low, `_determine_aggregate_payload_budget`
floors at `_MIN_AGGREGATE_UNCOMPRESSED_BYTES` (16 MiB) rather than
collapsing to zero — a deliberate tradeoff (a floor of zero would make
Agent Backup entirely non-functional on a memory-constrained host) that
carries a small residual OOM risk in a genuinely extreme low-memory
scenario; this is a documented limitation, not a silently-accepted gap.

## SQLite / strict-JSON / permissions / baseline / publication regression (Sections 36-40)

Re-run as part of the full focused suite (Section 39) after resource-
budget integration — no redesign, no regression. Permission modes (new
backup/replacement/rollback/recovery-artifact all `0600`) are unaffected;
this pass never touches any `os.chmod`/file-creation-mode code path.

## Focused suite (Section 44)

```text
tests/test_agent_backup.py
collected: 369
passed:    369
failed:    0
warnings:  1 (pre-existing, unrelated: a UserWarning from zipfile itself
              inside test_verify_rejects_duplicate_zip_member_name)
skips/xfailed: 0
runtime:   42.44s
native exit: 0
```

322 A2F9 tests + 47 net-new A2F10 tests = 369, verified by direct pytest
collection count, not inherited arithmetic.

**Not silently hidden:** across several runs during this pass's own
development, `test_sole_persona_replaced_with_unix_socket_fails_backup`
intermittently failed with `OSError: AF_UNIX path too long` (368 passed
/ 1 failed) — reproduced 3 times, always at the exact same position in a
full-file run. Root-caused, not assumed: it passes cleanly every time in
isolation, and the failure is purely a function of the ABSOLUTE PATH
LENGTH of pytest's own `tmp_path` (this test binds a real `AF_UNIX`
socket to a path under `tmp_path`, and Linux's `sun_path` has a hard
~108-byte limit) — which itself depends on how many prior local pytest
`pytest-of-<user>/pytest-N` basetemp directories already exist on this
host at run time, not on anything this pass changed. The clean run
recorded above (369/369, native exit 0) is the authoritative figure;
this is a documented, pre-existing, host-path-length-dependent
environmental artifact, not a regression A2F10 introduced.

## Complete release suite (Section 45)

```text
collected: 3930
passed:    3929
failed:    1 (the same test_sole_persona_replaced_with_unix_socket_fails_backup
              environmental flake described above -- confirmed unrelated,
              passes standalone and in the clean focused re-run above)
warnings:  1 (the same pre-existing zipfile UserWarning noted above)
skips/xfailed: 0
runtime:   477.22s (0:07:57)
native exit: 1 by pytest's own documented convention (1 = tests ran and
             one failed) -- not separately captured via $? for this
             specific run (its output was piped through `tail`, which
             would have masked pytest's own exit code); the clean,
             directly-$?-captured focused re-run above (exit 0) is the
             one this note actually verified a raw exit code for
```

`tests/test_review_panel.py` completed naturally (32/32, no stall, no
inferred green).

**Arithmetic, checked directly, not inherited:** this run's
`test_agent_backup.py` contribution is 369 (confirmed by direct
collection count above). A2F9's own note reported a full-suite total of
3885 with 322 of those in `test_agent_backup.py` (3885 − 322 = 3563
tests in every OTHER file). This run's total minus `test_agent_backup.py`
is 3930 − 369 = 3561 — TWO fewer than A2F9's own reported "other files"
figure, even though release HEAD (`ee40259`) and every file besides
`core/agent_backup.py`/`tests/test_agent_backup.py` are unchanged since
A2F9 ran (confirmed by this pass's own Section 0 gate). This 2-test
discrepancy was not chased down further: it lies entirely outside files
this campaign touched, and is out of scope for a ZIP-closure/durability-
truth/resource-budget pass. Recorded honestly as unreconciled rather than
forced to match the inherited number.

## The eighteen load-bearing architectural statements (cumulative)

Statements 1-14 (A2F7/A2F8/A2F9) are restated verbatim in
`core/agent_backup.py`'s own module docstring and remain unchanged,
independently re-verified by this pass's full focused-suite pass. Four
further statements this pass adds:

```text
15. Strict v1 ZIP verification accepts only ZIP structures the Lumina
    v1 builder can actually emit -- membership in a closed, source-
    vetted set (compression method, general-purpose flags, empty extra
    fields), never merely internal local/central agreement on an
    otherwise-impossible value.
16. Content-publication success and parent-directory durability
    confirmation are reported as separate, truthful facts everywhere
    this module surfaces either one (primary publication, rollback,
    recovery preservation) -- never silently conflated in either
    direction.
17. Agent Backup enforces a finite aggregate uncompressed-resource
    budget, charged incrementally as bytes are actually captured,
    before retaining further collected state or decompressing
    untrusted archive content beyond that same bound.
18. Every physical ZIP entry-count ceiling this module relies on is
    enforced by the BUILDER before construction, not discovered only
    after a self-contradictory archive has already been fully built.
```

Explicitly pinning the four statements named in the mission itself:

1. Strict v1 accepts only ZIP structures the Lumina v1 builder can
   emit — closed by membership in a source-vetted set, not mere
   local/central agreement.
2. Parent-directory fsync status is evidence, not something silently
   assumed; content success and durability confirmation are distinct
   facts, reported separately everywhere either is surfaced.
3. Agent Backup has a finite aggregate resource contract enforced
   before unsafe memory retention/decompression, on both the builder
   and verifier sides, sharing one operator-configurable ceiling.
4. Future performance work may reduce amplification, but v1 may never
   be unbounded — `AGENT-BACKUP-SPARSE-LARGEFILE-01` remains open,
   nonblocking, unchanged.

## STOP

No commit. No push. No mirror. No campaign cleanup. Candidate returned
for AGENT-BACKUP-RESTORE-A2R11.
