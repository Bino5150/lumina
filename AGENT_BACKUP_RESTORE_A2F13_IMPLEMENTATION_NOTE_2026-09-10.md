# AGENT-BACKUP-RESTORE-A2F13 — Multi-Mount cgroup v2 Resolution & Fail-Closed Memory-Signal Semantics

## Root cause

A2R13 found that `core/agent_backup.py`'s cgroup v2 discovery (`_read_cgroup_v2_mount`,
singular) unconditionally returned the *first* `cgroup2` record it found in
`/proc/self/mountinfo`, with no check for whether that record was actually
applicable to the current process's own cgroup path. On a host with more
than one `cgroup2` mount (a bind mount / duplicate view of the real
hierarchy is common), this could:

1. pick an unrelated mount (e.g. `root=/other.slice`),
2. fail to resolve the process's own cgroup directory under it (the
   process's logical path isn't beneath that mount's root), and
3. discard the real, applicable, tighter cgroup constraint sitting on a
   *later* mount record — collapsing to `None` ("no cgroup constraint")
   and silently falling back to host-only `/proc/meminfo` accounting.

Separately, `_read_memory_max_current` folded two genuinely different
situations into the same `None`: "this level explicitly has no ceiling"
(`memory.max == "max"`) and "this level could not be read at all"
(missing/malformed file). That ambiguity meant a *resolution failure*
could be merged upstream (`_effective_available_memory_bytes`) into an
effectively unconstrained working-set allowance — the fail-open pattern
this whole task set out to remove.

## Repair

Replaced the single-mount model in `core/agent_backup.py` end to end
(lines ~825–1146):

- **`_read_cgroup_v2_mounts()`** (was `_read_cgroup_v2_mount`, singular) —
  reads `/proc/self/mountinfo` once, returns **every** `cgroup2` mount
  record as a list, never just the first.
- **`_resolve_cgroup_mapping(logical_path, mount_root, mount_point)`** —
  pure, per-mount applicability + resolution. Applicability is decided on
  whole path components (`== root` / `startswith(root + "/")`), never a
  naive string prefix — `/user.slice2` is not beneath `/user.slice`.
- **`_resolve_process_cgroup_dir()`** — kept as a single-answer
  convenience wrapper (first *applicable* mount) for simple callers; the
  real availability computation never calls it.
- **`_classify_cgroup_memory_level(cgroup_dir)`** — tri-state single-level
  reader: `("finite", n)` / `("unlimited", None)` / `("unknown", None)`,
  replacing the old two-state `_read_memory_max_current`.
- **`_evaluate_cgroup_mapping(leaf_dir, mount_root, mount_point)`** — walks
  one mapping's ancestors from leaf to (and including) its own mount
  point, and classifies the whole mapping tri-state: any finite level
  wins (minimum across finite levels, preserving A2F12's "does not stop
  at first finite limit"); all-levels-explicitly-unlimited → `unlimited`;
  otherwise → `unknown`.
- **`_discover_cgroup_v2_availability()`** — the new top-level entry
  point. Reads the process's logical cgroup path and the full mount table
  **exactly once each** (one snapshot per invocation — Section 15), then
  evaluates *every* applicable mount's mapping and aggregates: any finite
  mapping present → tightest finite value wins overall; all-applicable-
  mappings-unlimited → `unlimited`; otherwise (including "no mount was
  applicable at all") → `unknown`.
- **`_effective_available_memory_bytes()`** — rewritten to consume the
  tri-state result directly instead of a collapsed `int | None`:
  - `finite(n)` → `N = min(host, n)` (host included when known).
  - `unlimited` → `N = host` when known, else the finite fallback.
  - `unknown` → **fail closed**: `N = min(host, _FALLBACK_AVAILABLE_MEMORY_BYTES)`
    when host is known, else the fallback outright — uncertainty is
    capped at the pre-existing 256 MiB conservative fallback, never
    widened to an abundant host figure. This is the literal A2R13 P0
    repair reaching the merge point.

### A second real bug found while live-verifying the fix

Running the new discovery chain against *this machine's own* real
`/proc/self/cgroup` / `/sys/fs/cgroup` (not a synthetic fixture) exposed a
second, genuine defect introduced by my own first draft of
`_evaluate_cgroup_mapping`: the true, whole-system **root** cgroup has no
`memory.max` file at all — confirmed directly:

```
$ ls /sys/fs/cgroup/memory.max
ls: cannot access '/sys/fs/cgroup/memory.max': No such file or directory
$ ls /sys/fs/cgroup/user.slice/memory.max
/sys/fs/cgroup/user.slice/memory.max
```

This is documented kernel behavior (`Documentation/admin-guide/cgroup-v2.rst`):
the root cgroup has no resource-control files because it has no concept of
its own limit. My first draft treated that absence the same as any other
missing/malformed level (`"unknown"`), which poisoned an otherwise fully
resolved, all-`unlimited` real-host mapping into `"unknown"` — triggering
the new fail-closed cap and *dropping this real host's effective memory
from ~3.9 GiB to the 256 MiB fallback* for no real reason. Live before/after:

```
# before fix                              # after fix
discover: ('unknown', None)               discover: ('unlimited', None)
effective: 268435456   (256 MiB, wrong)    effective: 4091555840  (~3.9 GiB, correct)
```

Fixed by scoping the true-root exception precisely: in `_evaluate_cgroup_mapping`,
when the walk's final level *is* the mount point **and** that mount's own
`mount_root` field is `"/"` (this mount exposes the entire real hierarchy,
not a namespaced/subtree-bound slice), a missing `memory.max` there
contributes nothing (neither finite nor "unknown") rather than poisoning
the mapping. A subtree-bound or namespaced mount's own top level is a
real, ordinary cgroup and does **not** get this exception — a missing
`memory.max` there is still treated as genuinely unresolvable. This one
regressed a passing test
(`test_replacement_verification_refuses_prior_large_uncompressed_content_on_low_memory_host`,
which runs against real host memory with no simulation) before the fix,
and the full suite is green with it in place.

## Tests

`tests/test_agent_backup.py`'s cgroup section (~625 lines) was rewritten
for the new API and expanded to cover:

- Every A2F12-era single-mount regression, re-expressed against the new
  function names/shapes (does-not-stop-at-first-finite, root-unlimited/
  nested-finite, root-broken/nested-finite, never-negative, three-level
  tightest-wins, mount-absent, path-unreadable).
- The literal A2R13 regression fixture (Section 12: unrelated first mount,
  applicable second mount, 384 MiB expected) — at both the raw discovery
  layer and end-to-end through `_effective_available_memory_bytes` /
  `_working_set_ceiling_bytes`.
- The full multi-mount matrix from Section 13: applicable-first-unlimited/
  second-finite, first-finite/second-tighter, subtree+root mount overlap,
  broken-first/valid-second, unrelated→broken→valid chains, all-unrelated
  (→ unknown), all-applicable-but-unreadable (→ unknown).
- Duplicate/bind views (Section 14): identical constraint on two mount
  points → no double-counting; differing constraint → tighter wins.
- One-snapshot-per-invocation (Section 15): a call-count assertion proving
  `_read_process_cgroup_v2_path`/`_read_cgroup_v2_mounts` are each called
  exactly once per `_discover_cgroup_v2_availability()` invocation, plus a
  stronger "mutating mock" test proving a would-be second generation of
  the mount table is never observed.
- The true-kernel-root exception (new, from the live-verification finding
  above): forgiven only when `mount_root == "/"` and the file is genuinely
  absent; still finds a genuine finite nested reading through it; **not**
  forgiven when `mount_root != "/"` (a regression guard against widening
  the exception too far).
- The full `_effective_available_memory_bytes` merge matrix from Section
  18: finite/host-tighter, finite/host-missing, unlimited/host-present,
  unlimited/host-missing, unknown/host-present (capped), unknown/
  host-missing (fallback), and unknown capping at `min(host, fallback)`
  rather than the bare fallback constant (a host smaller than the
  fallback must still win).

## Live process observation

Captured on this development host directly against real `/proc` state
(not a fixture) — see the before/after table above. Real logical cgroup
path observed: `/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-127092.scope`,
one real applicable mount (`root=/`, `mountpoint=/sys/fs/cgroup`), every
real ancestor level explicitly `unlimited`, terminal root level correctly
exempted. Final live result: `('unlimited', None)` → effective available
memory = real host `MemAvailable` (~3.9 GiB on this run).

## Working-set math (literal regression, Section 19)

Re-derived from the actual current code (not restated from the task),
using the A2R13 fixture's `N = 384 MiB`:

```
N = 402653184           (384 MiB)
W = 201326592           (192 MiB, N * 0.5)
build budget = 22369621 (matches task's "≈ 22,369,621 bytes" exactly)
verify limit = 67108864 (64 MiB, fixed)
```

## Focused-suite receipt

```
tests/test_agent_backup.py
collected: 464
passed:    464
failed:    0
warnings:  1 (pre-existing, unrelated — zipfile "Duplicate name: manifest.json"
              UserWarning from test_verify_rejects_duplicate_zip_member_name)
skips/xfailed: 0
runtime:   45.04s
native exit: 0
attempt:   first
```

(Not inherited from A2F12's 430 — this is the actual current collected
count, +34 net new/expanded cgroup tests.)

## Complete release-suite receipt

```
collected: 4027
passed:    4027
failed:    0
warnings:  1 (same pre-existing warning as above)
skips/xfailed: 0
runtime:   494.83s (0:08:14)
native exit: 0
attempt:   first
```

(Not inherited from A2F12's 3993 — actual current collected count.)

## Duck

```
skills/rubber-duck-debugging.md
sha256: d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be
present: yes
unchanged: yes (hash matches exactly)
captured: yes (working tree file, `git status` clean/quiet on it)
shipped baseline: no (zero commits touch this path in any branch —
  `git log --oneline --all -- skills/rubber-duck-debugging.md` is empty)
provenance: unknown (gitignored — `.gitignore:39:skills/rubber-duck-debugging.md`
  — sitting in the working tree outside version control)
```

Fourteen adversarial rounds now.

## Remaining limitations

- cgroup v1 (`memory.limit_in_bytes`/`memory.usage_in_bytes`) is still
  intentionally not source-vetted or handled — unchanged from A2F12's own
  stated scope; its absence still degrades to `unknown`, never to
  "unconstrained."
- The true-root exception is scoped to an exact `mount_root == "/"`
  string match on the mountinfo root field. A cgroup namespace that
  presents its own namespace root as `"/"` to the *inside* process while
  the outer mountinfo root field reads differently was not source-vetted
  live (no such namespace was available to test against on this host) —
  the exception simply won't fire in that case, which fails safe (falls
  through to normal `unknown`-on-missing-file handling) rather than
  fails open.
- No UI, no Restore, no commit, no push, no mirror — per task scope.

## Pinned statements

1. Mountinfo order is not authority — `_read_cgroup_v2_mounts()` returns
   every record; applicability is decided per-record against the
   process's own logical path, never by position.
2. Agent Backup evaluates every cgroup2 mount view applicable to the
   current process before deciding its memory constraint
   (`_discover_cgroup_v2_availability` aggregates *all* applicable
   mappings, tightest finite wins).
3. A successfully resolved unlimited cgroup (`"unlimited"`) and a failed/
   unknown cgroup resolution (`"unknown"`) are different security states,
   carried as distinct tags through the whole chain — never collapsed
   into the same `None`.
4. Unknown cgroup resolution never widens the working-set allowance to
   host memory alone — `_effective_available_memory_bytes` caps it at
   `min(host, _FALLBACK_AVAILABLE_MEMORY_BYTES)` instead.
5. Resolved leaf, mount boundary, and ancestor walk all derive from the
   same captured mountinfo record — `_evaluate_cgroup_mapping` takes its
   own mapping's `(mount_root, mount_point)` as a bound tuple, never mixes
   one mapping's leaf with another's boundary.

## STOP

No commit. No push. No mirror. No campaign cleanup.

Returning for **AGENT-BACKUP-RESTORE-A2R14**.
