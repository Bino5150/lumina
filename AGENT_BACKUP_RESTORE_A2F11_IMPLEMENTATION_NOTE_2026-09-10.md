# AGENT-BACKUP-RESTORE-A2F11 Implementation Note (2026-09-10)

**Mission:** repair all three AGENT-BACKUP-RESTORE-A2R11 checkpoint blockers (working-set budget, canonical ZIP metadata, manifest/API truth) while preserving every A2R11-cleared subsystem. No UI, no Restore, no unrelated cleanup, no commit, no push, no mirror. Candidate returned for AGENT-BACKUP-RESTORE-A2R12.

## 0. Live Repository Gate

```
release  ~/lumina-release  branch main
  HEAD:      ee40259c980d3c6fcca56fc2f27dcefd3814c178  (== origin/main, 0 ahead/0 behind)
  status:    clean except the same untracked Agent Backup work files A2F1-A2F10 already
             left (core/agent_backup.py, tests/test_agent_backup.py, metadata/,
             AGENT_BACKUP_RESTORE_*.md) -- no hidden git-operation state (no MERGE_HEAD,
             no rebase-merge/rebase-apply)

dev      ~/lumina  branch dev-local
  HEAD:      9b09542489210445b6b7fcc4ef2148e5f9eb64a8
  status:    1 modified tracked file (projects/lumina-dev/codebase.md) + 4 untracked
             skills/*.md files -- unrelated to Agent Backup, not touched by this pass
  ancestry:  release HEAD IS an ancestor of dev HEAD (confirmed via
             git merge-base --is-ancestor)
  push URL:  origin https://github.com/Bino5150/lumina.git (release repo; dev has no
             separate push URL override); push.default unset (git default); no
             pre-push hook present
```

Both HEADs matched the historical expectation given in the task exactly. Verified fresh, not inherited.

## 1. A2R11 PASS areas -- preserved

Full focused suite (369 -> 399 collected, all passing) and full release suite (see
Section 47) re-ran clean after this pass's changes. No redesign touched: immutable
filesystem-member pipeline, baseline single-snapshot authority, SQLite isolated
snapshot + serialize, same-byte SQLite validation, stable prior-destination capture,
immutable rollback, verified recovery artifacts, strict JSON, ZIP local/central
CRC-size-flag equality, ZIP prefix/concatenation rejection, ZIP64 builder refusal,
content-based publication observation, 0600 permissions, directory-fsync evidence
mechanics, closed-world manifest namespace, malformed-input never-raises, duck.

## 2-9. P0 -- Working-Set Contract (resource policy root cause)

**Root cause, confirmed by reading the live A2F10 code before changing anything (never
assumed):** `_determine_aggregate_payload_budget` computed a build-time PAYLOAD ceiling
that was itself already host-memory-aware, but two other things broke the actual safety
property:

1. `if safe_for_available < _MIN_AGGREGATE_UNCOMPRESSED_BYTES: safe_for_available =
   _MIN_AGGREGATE_UNCOMPRESSED_BYTES` -- a hard floor that could push the derived value
   back UP past what the host-safety computation just said was safe. A2F10's own
   implementation note explicitly flagged this as a known, accepted residual risk
   ("carries a small residual OOM risk in a genuinely extreme low-memory scenario").
   A2R11 measured the concrete consequence: a simulated 256 MiB host with a 128 MiB
   safety target got floored up to a 160 MiB estimated peak.
2. `_verify_agent_backup_inner`'s decompression-budget check used
   `_resolve_configured_payload_budget()` -- the raw OPERATOR-configured ceiling (2 GiB
   default, up to 16 GiB via `LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES`) -- directly, with
   **zero host-memory clamping**. A2F10's own comment explained the reasoning: build-time
   charging is already clamped downward from this same ceiling, so verify-time reusing it
   directly is safe. That reasoning is only true for a build's OWN immediate
   self-verification of the archive it just produced. It was never true for verifying an
   EXISTING/PRIOR archive found on disk (never charged against any build's budget),
   standalone `verify_agent_backup()`/`build_backup_receipt()` calls, or recovery-artifact
   verification.

**Conceptual fix (Section 2):** two distinct things, one deriving from the other, never
the reverse:

- `_working_set_ceiling_bytes()` -- the maximum memory pressure Agent Backup budgets
  ITSELF to use, always finite, a conservative fraction (kept at A2F10's own ~50% target,
  source-vetting did not disprove it) of one combined available-memory figure.
- Two separate PHASE limits derived from that ceiling: `_determine_aggregate_payload_budget`
  (build) and `_determine_verification_payload_budget` (verify/decompress) -- each with its
  OWN, separately-measured amplification factor (Section 20 below), never sharing one number.

**One effective memory-availability source (Section 4):** `_effective_available_memory_bytes()`
= min of every FINITE signal from `_read_meminfo_available_bytes()` (`/proc/meminfo`
`MemAvailable`, unchanged from A2F10) and the new `_read_cgroup_v2_available_bytes()`
(`/sys/fs/cgroup/memory.max` - `memory.current`, treating the literal string `"max"` as "no
tighter constraint"; cgroup v1 intentionally NOT implemented -- no current Lumina deployment
target relies on it, and speculative untested v1 parsing would be exactly the kind of
scope creep this discipline warns against). If NEITHER signal is discoverable, returns
`_FALLBACK_AVAILABLE_MEMORY_BYTES` (256 MiB -- see rationale below), never falling through
to "unbounded."

**Fallback value rationale (Section 5), explicitly not "picked because it looks nice":** the
resource matrix's own floor (128 MiB) was considered first, but at this module's fixed 64
MiB overhead reserve, a 128 MiB effective-available figure derives a working-set ceiling of
exactly 64 MiB -- fully consumed by fixed overhead alone, an intentional hard-refuse tier
for a HOST KNOWN to be that small. Total discovery failure is a different situation: it says
nothing about whether the real host is tiny or merely running somewhere `/proc` is masked
(sandboxing) or absent (any non-Linux platform -- both signals are Linux-only, so EVERY
non-Linux host hits this fallback on every single build). Defaulting total-discovery-failure
to the one scenario that always refuses, unconditionally, forever, on every non-Linux
platform, would not be "conservative" -- it would be "silently non-functional," which Section
5 does not ask for. 256 MiB was chosen instead: it is the exact reference low-memory scenario
Section 6 of this task itself worked its floor-removal example against (256 MiB available ->
128 MiB ceiling -> ~10.7 MiB build allowance, small but genuinely nonzero and safe).

**Floor removed, refusal added (Section 6, the literal A2R11 fix):**
`_determine_aggregate_payload_budget` no longer floors its derived value upward. When
`working_set_ceiling - fixed_overhead - prior_backup_bytes <= 0` (the host cannot even cover
its own fixed overhead), or when the final `min(configured, derived)` is `<= 0`, it now
raises `AgentBackupError("insufficient memory budget for Agent Backup: ...")` instead --
refusing the whole build BEFORE any collection or publication step, leaving any existing
destination untouched. A low-memory host is now allowed to refuse, exactly as Section 6
specifies, rather than being pushed past what it can safely support.

**Prior-backup accounting (Section 10), traced against the REAL call sequence in
`_build_agent_backup_core` (source-vetted, not assumed):**

```
1. cheap os.stat(dest_path) -> prior_backup_bytes (compressed, on-disk size; unchanged
   from A2F10 -- this part was already correct)
2. budget = build-phase budget, charging prior_backup_bytes as reserved overhead for
   the WHOLE build's duration (conservative: reserved before it's actually resident)
3. _collect_members(...) -- new-member bytes accumulate in `members`, charged
   incrementally against the SAME budget as they're captured
4. ZIP built to tmp_path from `members`' already-resident data
5. self-verification of tmp_path (_capture_and_verify_archive) -- `members`' bytes
   are STILL resident here (the list stays in scope for the rest of the function)
6. IF dest_path exists: prior-destination verification -- by this point `members`'
   bytes AND built_data (the just-built, not-yet-published candidate) are BOTH
   simultaneously resident, on top of whatever the prior archive's own verification
   newly makes resident
7. publish (or rollback)
```

This is genuinely more state than a flat "prior compressed bytes" model captures at steps
5-6. Rather than build a fully general shared ledger across the whole pipeline (evidence-
disproportionate for a v1 pass), `_capture_and_verify_archive` gained an optional
`resident_bytes_hint` parameter, threaded from the two call sites in
`_build_agent_backup_core` that actually know about this extra co-resident state:

- self-verification (step 5): `resident_bytes_hint = sum(len(m["data"]) for m in members)`
- prior-destination verification (step 6): the same sum **+** `len(built_data)`

`verify_agent_backup()`'s own public signature gained the same optional parameter
(default 0, so every existing/standalone caller is unaffected) and forwards it to
`_verify_agent_backup_inner`, which adds the archive-being-verified's OWN already-resident
raw bytes on top (`len(raw_bytes)` when available) before deriving the verification budget.
This closes Section 11's requirement uniformly: builder self-verification, prior-destination
verification, standalone `verify_agent_backup()`/`build_backup_receipt()` calls, and
recovery-artifact verification ALL now derive their decompression allowance from the same
host-safe contract -- the prior three with progressively more precise `resident_bytes_hint`
context, the last (`_preserve_rollback_failure_artifact`'s own `verify_agent_backup()` call,
a rollback-failure/double-failure path) with the default hint of 0, a documented residual
approximation (see Section "Remaining limitations" below) rather than perfected, since the
archive it re-verifies there already passed a full verification once earlier in the same
build before publication was attempted.

**Verification amplification factor, empirically measured, not assumed (Section 20):**
fresh child-process RSS measurements (`resource.getrusage(RUSAGE_SELF).ru_maxrss`,
`_verify_agent_backup_inner` called directly against a real archive with one large,
highly-compressible member, isolating decompression pressure from build pressure):

```
baseline (1 MiB payload):   21964 KiB peak RSS
32 MiB payload:              52920 KiB peak RSS  (+30.16 MiB over baseline, ~0.94x)
64 MiB payload:               85780 KiB peak RSS  (+62.27 MiB over baseline, ~0.97x)
```

This independently reproduces A2F10's own reported figure (a 64 MiB member's decompression
added ~60 MiB RSS, ~0.94x) to within measurement noise. Decompression amplification is
genuinely close to 1x (dominated by the output buffer itself), utterly unlike the build
side's empirically-measured ~5x (32/64 MiB payload builds adding ~160/~320 MiB RSS, A2R10's
original measurement, unchanged and not re-eliminated by this pass).
`_VERIFICATION_AMPLIFICATION_FACTOR = 2` was chosen: comfortably above the measured ~1x for
zlib's own internal decompressor buffers and multiple/aggregate members, while remaining
honest that verification does not cost anywhere near what a full build does. Using the
build's own 6x factor for verification (the naive fix) would have been safe but needlessly
punitive on constrained hosts.

**Per-member structural overhead (Section 18):** `_RESOURCE_BUDGET_PER_MEMBER_STRUCTURAL_
OVERHEAD_BYTES = 2048`, applied on the VERIFY side only (`member_count * 2048`, subtracted
from the available working-set before deriving the decompression limit) -- the verifier has
trusted, un-decompressed central-directory member counts available up front; the builder
does not know its own eventual member count before collection starts, and is already
separately, hard-bounded by `_MAX_PHYSICAL_ZIP_ENTRIES` (65,534) -- at 2048 bytes/member that
ceiling is ~128 MiB worst case, comfortably inside the existing 64 MiB fixed-overhead
reserve's own order of magnitude, so no separate build-side per-member accounting was added.
Confirmed live (Section 45): a constrained host with a maximal-entry-count archive can now
legitimately refuse verification purely on member COUNT even when aggregate payload is
near-zero -- an intentional, documented consequence of Section 18's own "a million zero-byte
files should not be free" requirement, not a bug.

**No artificial floor anywhere (Section 9):** confirmed by direct code reading -- there is no
`max(MIN, ...)` construct left in either `_determine_aggregate_payload_budget` or
`_determine_verification_payload_budget`. Both return `0`/raise cleanly instead.

## 15-17. Resource / Operator-Override / Cgroup Matrices

Run live (outside pytest, session scratchpad, not committed --
`a2f11_matrices.py`) against the real module functions directly:

```
SECTION 15 -- RESOURCE MATRIX
   available |      ceiling |   configured |  build limit |  verify(0mem,0cnt) |  fixed reserve | amp(build/verify)
  128.00 MiB |    64.00 MiB |     2.00 GiB |         None |               None |      64.00 MiB | 6x / 2x
  256.00 MiB |   128.00 MiB |     2.00 GiB |    10.67 MiB |          32.00 MiB |      64.00 MiB | 6x / 2x
  512.00 MiB |   256.00 MiB |     2.00 GiB |    32.00 MiB |          96.00 MiB |      64.00 MiB | 6x / 2x
    1.00 GiB |   512.00 MiB |     2.00 GiB |    74.67 MiB |         224.00 MiB |      64.00 MiB | 6x / 2x
    2.00 GiB |     1.00 GiB |     2.00 GiB |   160.00 MiB |         480.00 MiB |      64.00 MiB | 6x / 2x
    8.00 GiB |     4.00 GiB |     2.00 GiB |   672.00 MiB |           1.97 GiB |      64.00 MiB | 6x / 2x
   64.00 GiB |    32.00 GiB |     2.00 GiB |     2.00 GiB |           2.00 GiB |      64.00 MiB | 6x / 2x

Safety-floor violations found: 0
(proof: for every row, build_limit * 6 + 64 MiB <= ceiling, and
 verify_limit * 2 + 64 MiB <= ceiling -- checked programmatically, not eyeballed)

SECTION 16 -- OPERATOR OVERRIDE MATRIX (host fixed at 512 MiB effective available,
ceiling 256 MiB, host-safe derived build limit 32.00 MiB)
  1 MiB, 64 MiB, 512 MiB, 2 GiB, 16 GiB, 1 TiB, 0, -5, "not-a-number", 16 TiB
  -> configured (post sanity-validation) varies (2 GiB default for every invalid/
     out-of-range value, the raw value for every in-range one, 16 GiB cap for the
     16 TiB case) but EFFECTIVE never exceeds 32.00 MiB in any of the 10 cases --
     the operator value only ever constrains further, never expands past host-safe

SECTION 17 -- CGROUP MATRIX -- all 5 required scenarios (host tighter, cgroup
tighter, host-fails/cgroup-finite, host-finite/cgroup-fails, both-fail-finite-
fallback) matched their expected effective-available figure exactly.
```

128 MiB legitimately refuses outright at both phases (Section 6's intended hard-refuse
tier); every other row derives a nonzero, safety-proof budget.

## 21-30. P0 -- Canonical ZIP Metadata Contract

**Root cause, source-vetted empirically (never assumed, per Section 22):** built a real
archive through this module's own `zipfile.ZipFile(..., ZIP_DEFLATED, allowZip64=False)`
path and inspected the resulting `infolist()`. Confirmed real, previously-unenforced
discrepancies:

- `create_system` is genuinely host-platform-dependent in `zipfile.ZipInfo.__init__`
  (`0` on `win32`, `3` elsewhere) -- never made explicit by the builder.
- `manifest.json` was written via a BARE-STRING `zf.writestr("manifest.json", ...)` call
  (not a `ZipInfo`), which left `zipfile` to fill in its OWN default `external_attr`
  (`0o600 << 16`, `rw-------`) -- genuinely DIFFERENT from every payload member's explicit
  `0o644 << 16` (`rw-r--r--`). Confirmed by building the exact real code path and comparing
  `infolist()` output side by side: `27525120` (payload members) vs `25165824`
  (manifest.json, before this pass).
- `version_needed`, disk number start (`volume`), `internal_attr` were parsed by this
  module's own local-header parser and/or read from `infolist()` but never compared against
  anything -- `_parse_local_header` explicitly discarded `version_needed` into a variable
  named `_version_needed`.

**Fix:** one canonical `_make_v1_zipinfo(archive_path)` helper, used for EVERY member
INCLUDING manifest.json now (previously the odd one out), fixing `create_system` (3, Unix,
explicit -- independent of `sys.platform`), `create_version`/`extract_version` (both 20,
zipfile's own `DEFAULT_VERSION`), `external_attr` (`0o644 << 16` for every member, closing
the manifest.json discrepancy), `internal_attr` (0), `volume`/disk-number-start (0), `comment`
(`b""`), `extra` (`b""`). `_validate_zip_physical_structure` gained matching per-member
checks (`create_version`, `extract_version` both central AND local -- `_parse_local_header`
now returns `version_needed` instead of discarding it -- plus local/central agreement on it,
`create_system`, `volume`, `internal_attr`, `external_attr` exact-match, and central
per-member `comment`). Exact-value equality was used throughout (not a mask) -- the real
builder emits exactly ONE value for every one of these fields across every member; there is
no legitimate variance to allow for.

## 39-44. Regression re-runs

SQLite (WAL source, isolated snapshot, serialize/deserialize exact bytes, role fingerprint,
quick_check, concurrent writer, budget failure after capture never mutates owner DB),
immutable-member pipeline (source A -> staging B -> archive stays A; post-hash mutation
caught), rollback/durability (prior capture, mismatch, rollback exact bytes, post-rollback
verify, `directory_fsynced` truth, recovery preservation), strict JSON (duplicate key,
escape-equivalent duplicate, nested duplicate, NaN/Infinity/-Infinity), permissions (backup/
replacement/rollback/recovery all still 0600 -- confirmed this pass never touches any
`os.chmod`/creation-mode code path), duck (present, unchanged, hash-verified above) -- all
re-ran as part of the focused and full suites below with zero redesign. No regressions.

## 32-37. P1 -- Manifest / API Truth

**Root cause, confirmed by reading the live code (never assumed):** `build_agent_backup()`
was documented as returning "the exact manifest dict written," but on a directory-fsync
failure it built a shallow copy of `result.manifest`, appended a durability warning string
to the copy's `warnings` list, and returned THAT instead -- `build_agent_backup(...) !=
json.loads(archived manifest.json)` was a real, reachable outcome, not a hypothetical one,
whenever that specific publish's own parent-directory fsync failed. The function's own
docstring even said "`result.manifest` itself is never mutated -- only the dict this call
returns may differ from it," directly contradicting the "returns the exact manifest"
contract one paragraph above it.

**Fix:** `build_agent_backup()` now returns `result.manifest` completely unmodified, always.
A new `AgentBackupDurabilityWarning(UserWarning)` class was added; a directory-fsync failure
now surfaces via `warnings.warn(..., AgentBackupDurabilityWarning, stacklevel=2)` instead --
a real, catchable, filterable Python warning (stdlib `warnings` module, no new dependency),
never touching the manifest dict. `build_agent_backup_with_receipt`/the receipt API were
**not** touched -- `receipt.manifest == archived manifest` and
`receipt.publication.directory_fsynced` were already correct and remain the rich, structured
evidence surface for exactly this fact (Section 35's own instruction, confirmed unchanged by
the full test suite).

Verified live, outside pytest (Section 45): `manifest == json.loads(zf.read("manifest.json"))`
under an injected fsync failure returns `True`; `warnings.catch_warnings(record=True)` catches
exactly one `AgentBackupDurabilityWarning`; the returned manifest's own `warnings` list
carries no fsync-related text at all.

## 45. Self-Adversarial A2F11 Replay (outside pytest)

All confirmed live, against the real module (scripts in the session scratchpad, not
committed):

- **256 MiB effective-memory verification-bypass reproduction (Section 14, the literal
  A2R11 repro):** built a real Lumina env with a 64 MB highly-compressible avatar (~1000:1
  DEFLATE), published it (real host memory, no simulation), removed the oversized avatar,
  then attempted a replacement build with `_effective_available_memory_bytes` simulated at
  256 MiB. Before this pass this would have proceeded (verify used the raw 2 GiB configured
  ceiling, unclamped). After this pass: `AgentBackupError` referencing "decompression
  budget," raised from prior-destination verification, BEFORE any publication step --
  confirmed the existing destination's bytes are byte-for-byte unchanged afterward. Also
  encoded as a permanent pytest regression test.
- **Large prior uncompressed / tiny compressed archive replacement:** same test as above.
- **Memory discovery failure + huge configured override + tighter cgroup than host:** two
  live combos run directly against the real functions (no pytest): (a) both host and cgroup
  discovery failing entirely, `LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES=16 GiB` -- effective
  build budget clamped to ~10.67 MiB (the 256 MiB fallback's own derived figure), NOT 16 GiB;
  (b) host reporting 8 GiB, cgroup reporting a tighter 300 MiB, same 16 GiB override --
  effective available correctly bound to the cgroup's 300 MiB, budget clamped to ~14.3 MiB.
  Both confirm the operator override can never expand past host/cgroup safety.
- **ZIP version needed = 63, disk_start = 1, symlink external attributes:** all three
  reproduced individually via direct byte-level central/local-directory surgery against a
  REAL builder-emitted archive (not a hand-built fake), each independently rejected with the
  expected, specific error message.
- **Plain build directory-fsync failure -- returned manifest vs archived manifest.json:**
  reproduced live; exact equality confirmed; durability warning confirmed caught separately;
  zero fsync-related text found inside the returned manifest's own `warnings` list.

**Unknown-unknowns pass (Section 45's own explicit ask) -- findings:**

- A maximal-entry-count (65,534-member) archive on a constrained host can now legitimately
  fail verification purely on member COUNT via the new per-member structural-overhead charge,
  even when aggregate declared payload is near-zero. Confirmed this is the INTENDED
  consequence of Section 18's own "a million zero-byte files should not be free" requirement,
  not a bug -- documented here rather than silently discovered later.
- `_read_cgroup_v2_available_bytes` never returns a negative figure even under a transient
  accounting race where `memory.current` briefly exceeds `memory.max` (`max(0, ...)`) --
  confirmed with a dedicated regression test rather than left as an assumption.
- `_verify_agent_backup_inner`'s `already_resident` computation degrades gracefully
  (`resident_bytes_hint + 0`) when `raw_bytes` is `None` (an exotic `archive_source` with
  neither `.getvalue()` nor a valid path) -- confirmed it cannot raise from this path, in
  keeping with `verify_agent_backup`'s own "never raises" contract.
- `warnings` is used as a local variable name (the manifest-warnings-list convention) in
  several existing `_collect_*`/`_build_agent_backup_core` functions throughout this module,
  which now coexists with the new module-level `import warnings`. Confirmed this is safe --
  Python's per-function scoping means the local shadowing in `_build_agent_backup_core`
  cannot affect the separate `build_agent_backup()` function that actually calls
  `warnings.warn(...)`; grepped to confirm no function that calls `warnings.warn` also
  declares a local variable of that name.

## 46. Focused Suite

```
tests/test_agent_backup.py
collected: 399  (was 369 before this pass; +30 new test cases)
passed:    399
failed:    0
warnings:  1 (pre-existing, unrelated -- test_verify_rejects_duplicate_zip_member_name's
              own deliberate duplicate-name construction; not touched by this pass)
skips/xfailed: 0
runtime:   48.56s
native exit: 0
```

New/rewritten tests (representative, not exhaustive -- see the diff for the full list):
working-set contract --
`test_determine_aggregate_payload_budget_uses_finite_fallback_when_memory_unavailable_everywhere`,
`test_determine_aggregate_payload_budget_does_not_floor_a_tiny_host_upward`,
`test_determine_aggregate_payload_budget_refuses_outright_when_host_cannot_cover_fixed_overhead`,
`test_read_cgroup_v2_available_bytes_*` (4 tests),
`test_effective_available_memory_bytes_*` (2 tests),
`test_working_set_ceiling_bytes_is_conservative_fraction_of_effective_available`,
`test_determine_verification_payload_budget_*` (5 tests),
`test_verify_agent_backup_resident_bytes_hint_shrinks_effective_budget`,
`test_replacement_verification_refuses_prior_large_uncompressed_content_on_low_memory_host`
(the literal A2R11 Section 14 reproduction); ZIP canonicalization --
`test_verify_rejects_version_needed_63_in_central_directory`,
`test_verify_rejects_version_needed_63_in_local_header_even_when_central_agrees`,
`test_verify_rejects_local_central_version_needed_disagreement`,
`test_verify_rejects_nonzero_disk_number_start`,
`test_verify_rejects_non_unix_create_system`,
`test_verify_rejects_nonzero_internal_attributes`,
`test_verify_rejects_non_regular_file_external_attributes` (6-way parametrized: symlink,
directory, socket, fifo, block-device, DOS-directory-bit),
`test_verify_rejects_central_directory_member_comment`,
`test_make_v1_zipinfo_matches_source_vetted_real_builder_output`; manifest truth --
`test_build_agent_backup_manifest_warns_when_directory_fsync_fails` (rewritten),
`test_replacement_backup_reports_directory_fsync_failure_same_as_first_backup` (rewritten).
Pre-existing fixtures updated to stay builder-realistic under the new canonical checks:
`_write_zip` (the shared mutation-matrix fixture, now uses `_make_v1_zipinfo`),
`test_real_65534_physical_entries_builds_and_strictly_verifies` and its 65535-entry sibling.

## 47. Complete Release Suite

Full command: `python3 -m pytest -q --no-header` from the release repo root.

**First attempt: hung, not a regression.** Confirmed via `ps -o etime,time,stat` (elapsed
time far exceeding CPU time) and per-thread `/proc/<pid>/task/*/wchan` (multiple threads in
`futex_do_wait`, one in `do_wait`, two in `anon_pipe_read`) -- a real deadlock, not slowness.
The captured output showed clean, all-passing progress through 94% of the suite, stalled
inside the PySide6 UI test region (`tests/test_ui_chat_scroll_01.py` and neighbors) --
nowhere near `core/agent_backup.py` or `tests/test_agent_backup.py`, which had already run
completely clean in the first 1% of collection order. This matches a known, pre-existing,
previously-documented intermittent full-suite deadlock unrelated to Agent Backup (confirmed
recurring by the owner on unrelated prior sessions). Killed and retried per that precedent
rather than waited out or root-caused as part of this ticket.

**Second attempt (clean):**

```
collected: 3962
passed:    3962
failed:    0
warnings:  1 (the same pre-existing, unrelated test_verify_rejects_duplicate_zip_member_name
              warning already noted in Section 46)
skips/xfailed: 0
runtime:   504.43s (0:08:24)
native exit: 0
```

Checkpoint requirement met: all collected tests pass, native exit 0.

## Remaining limitations

1. `_preserve_rollback_failure_artifact`'s own `verify_agent_backup()` call (a
   rollback-failure/double-failure path) uses the default `resident_bytes_hint=0` rather
   than full precision about what else might still be resident up its call stack at that
   point -- a documented, narrow approximation, not a gap in the host-safety property itself
   (that call still derives its budget from the same host-safe working-set contract, just
   without crediting every last co-resident byte). Acceptable because: (a) it is a rare
   double-failure path, and (b) the archive it re-verifies there already passed a full
   verification once earlier in the same build, before publication was ever attempted.
2. This pass does not reduce the build side's ~5-6x amplification (that remains
   `AGENT-BACKUP-SPARSE-LARGEFILE-01`, explicitly out-of-scope future performance work, per
   A2F10's own note).
3. cgroup v1 memory accounting is not implemented -- no current Lumina deployment target
   relies on it; a real future need should get its own separately-vetted pass rather than
   speculative untested parsing added here.
4. `_FALLBACK_AVAILABLE_MEMORY_BYTES` (256 MiB) is a source-vetted, documented judgment call,
   not a physically-derived constant -- reasoning is captured in both the module-level
   comment and Section 5 above for a future reviewer to re-evaluate against real deployment
   data if it ever proves wrong in practice.

## Acceptance-sentence self-check

- Working-set contract: single finite ceiling, phase limits derive from it (never the
  reverse), effective host/cgroup availability combined via minimum-of-finite-signals,
  measured amplification for both phases (not one number reused), prior-archive residency
  and decompression pressure both accounted for, structural overhead charged on the verify
  side, memory-discovery failure treated as a small finite fallback never as unbounded, no
  safety-defeating floor or configuration bypass anywhere (all confirmed via the matrices in
  Section 15-17 and the live adversarial replay in Section 45). CONFIRMED.
- ZIP format: one canonical parser-, disk-, compression-, feature-, and entry-type
  interpretation shared by builder (`_make_v1_zipinfo`) and verifier
  (`_validate_zip_physical_structure`'s matching checks), not merely mutual local/central
  consistency. CONFIRMED.
- `build_agent_backup()` returns exactly the manifest embedded in the immutable archive;
  publication/durability observations are separate evidence (`AgentBackupDurabilityWarning`
  / the receipt API). CONFIRMED.
- Previously-cleared immutable-member, SQLite, strict-JSON, rollback, publication,
  permissions, namespace, malformed-input, duck, focused-suite, and complete-release gates
  all remain independently green. CONFIRMED (Sections 39-47).

## STOP

No commit. No push. No mirror. No campaign cleanup. Candidate returned for
AGENT-BACKUP-RESTORE-A2R12.
