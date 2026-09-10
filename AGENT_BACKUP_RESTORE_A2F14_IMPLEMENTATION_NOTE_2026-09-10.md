# AGENT-BACKUP-RESTORE-A2F14 — Implementation Note (2026-09-10)

Repairs the two A2R14 P0 findings in `core/agent_backup.py`'s cgroup v2
evidence model: monotonic aggregation (P0-1) and root-identity recognition
(P0-2). Scope per the task scroll: cgroup evidence model only. No UI, no
Restore, no unrelated cleanup, no commit, no push, no mirror.

## 0. Live gate

- release HEAD/origin: `ee40259c980d3c6fcca56fc2f27dcefd3814c178` — confirmed, matches expected.
- dev HEAD: `9b09542489210445b6b7fcc4ef2148e5f9eb64a8` — confirmed, matches expected.
- Release status: only the expected untracked implementation-note/handoff files and `metadata/`; no stash, no hidden state.
- Dev repo (`~/lumina`): `release-local` remote push disabled, `push.default=nothing`, executable pre-push hook blocking pushes — all confirmed unchanged. Dev's own unrelated working-tree changes (`projects/lumina-dev/codebase.md`, untracked `skills/*.md`) left untouched.

## 1. A2R14 root cause

**P0-1 (non-monotonic aggregation).** Both accumulation sites collapsed to
`"finite"` the moment *any* reading in the set was finite, discarding any
coexisting `"unknown"` reading outright:

```python
# _evaluate_cgroup_mapping (per-level, one ancestor walk)
if finite_levels:
    return ("finite", min(finite_levels))
if any_unknown:
    return ("unknown", None)

# _discover_cgroup_v2_availability (per-mapping, across mounts)
if finite_values:
    return ("finite", min(finite_values))
if all(state == "unlimited" for state, _ in mapping_results):
    return ("unlimited", None)
return ("unknown", None)
```

An unreadable applicable constraint can conceal an arbitrarily tighter real
limit; a clean reading found *elsewhere* in the same walk or fold can never
license discarding it. This defeated the entire purpose of the `UNKNOWN`
state A2F13 introduced.

**P0-2 (slash-as-root authority).** `mount_root == "/"` alone was treated as
proof of the true, whole-system cgroup root:

```python
at_true_kernel_root = current_dir == mount_point_norm and mount_root_norm == "/"
if at_true_kernel_root and not os.path.exists(memory_max_path):
    pass  # forgiven as root
```

A cgroup namespace can make an ordinary, non-root global cgroup display as
`/` to every process confined inside it (`cgroup_namespaces(7)`); both
`/proc/self/cgroup` and mountinfo's own root field are captured from
*inside* that same namespace, so they display `/` identically for a true
root and for a namespace-remapped non-root cgroup. Pathname alone can never
distinguish them. `not os.path.exists(...)` compounded this by collapsing
"confirmed missing" and "exists but permission-denied/other I/O error"
into the same `False`.

## 2. New evidence representation

No public API change — `_evaluate_cgroup_mapping` and
`_discover_cgroup_v2_availability` keep their existing `(state, value)`
tri-state contract (`"finite"|"unlimited"|"unknown"`, `int | None`). The
one representational change: **`"unknown"` can now carry a non-`None`
value** — the tightest known-finite figure some other, independently
resolved level/mapping still managed to observe, retained alongside the
uncertainty rather than discarded by it. This directly satisfies Section
3's requirement to keep both facts representable ("known finite constraint
exists AND some applicable authority remains unresolved") without a new
type.

### `_finalize_cgroup_states(results: list[(state, value)]) -> (state, value)`

The one monotonic combination rule, shared by both accumulation sites
(per-level within one ancestor walk, and per-mapping across applicable
mounts):

```python
finite_values = [v for _, v in results if v is not None]   # from ANY state tag
tightest = min(finite_values) if finite_values else None
if any(s == "unknown" for s, _ in results):
    return ("unknown", tightest)
if tightest is not None:
    return ("finite", tightest)
return ("unlimited", None)
```

Built from `min()`/`any()` over the *whole* set rather than a pairwise
left-to-right fold, so it is inherently order-independent (Section 7) —
permuting the input list can never change the output; confirmed directly
by `test_finalize_cgroup_states_is_permutation_invariant`.

One bug caught and fixed during implementation, before it shipped: the
first draft collected `finite_values` only from entries tagged
`"finite"`, missing that this function is used **recursively** — the
cross-mapping fold combines *already-finalized* per-mapping results, which
can themselves be `("unknown", <value>)`. Filtering on `state == "finite"`
alone silently re-introduced the same discard bug one recursion level up
(caught by `test_discover_cgroup_v2_availability_applicable_subtree_and_root_mounts`
failing with `('unknown', None)` instead of the expected retained value).
Fixed by collecting a candidate value from *every* reading that carries
one, regardless of its own state tag.

### Same-mapping behavior (`_evaluate_cgroup_mapping`)

Collects every level's own `_classify_cgroup_memory_level` reading into a
list across the ancestor walk (instead of the old `finite_levels`/
`any_unknown` pair, discarded on the "if finite_levels" branch), then
calls `_finalize_cgroup_states` once at the end.

### Cross-mapping behavior (`_discover_cgroup_v2_availability`)

Collects every applicable mount's own `_evaluate_cgroup_mapping` verdict
into a list, then calls the same `_finalize_cgroup_states`.

### Monotonicity invariant

`cap(S + UNKNOWN) <= cap(S)` and `cap(S + FINITE(x)) <= cap(S)` for every
`x`, at the effective-bytes level — verified directly by
`test_monotonicity_adding_unknown_never_widens_effective_bytes` and
`test_monotonicity_adding_finite_never_widens_effective_bytes`, each
parametrized across 8 representative base evidence sets (and, for the
finite case, 4 values of `x` spanning tighter-than-everything to
looser-than-everything present).

### Fallback interaction

`_effective_available_memory_bytes`'s `"unknown"` branch changed from
`min(host, fallback)` (discarding `cgroup_value` outright) to
`min(fallback, host, cgroup_value)` — filtering out whichever of
`host`/`cgroup_value` is `None`. A retained known-finite figure can now
only ever *narrow* the result, never widen it past what fallback/host
alone would already give (confirmed: a known 1 GiB figure paired with a
256 MiB fallback still yields 256 MiB — the fallback is tighter; a known
64 MiB figure paired with the same fallback yields 64 MiB — the known
figure is tighter. Both are literal Section 17 examples).

## 3. Why "/" is not root authority; root-control source vet

Verified live against this sandbox's own real `/sys/fs/cgroup` (both via
`ls` and via `open()` with explicit `FileNotFoundError` inspection):

| file | true root (`/sys/fs/cgroup`) | non-root leaf |
|---|---|---|
| `memory.max` | ENOENT | present, e.g. `"max"` |
| `memory.current` | ENOENT | present |
| `cgroup.type` | **ENOENT** | **present, `"domain"`** |
| `cgroup.controllers` | present | present |

`cgroup.controllers` exists on both and so is useless as a discriminator.
`cgroup.type` is documented (`cgroups-v2.rst` §5.5, "A read-write single
value file which exists on non-root cgroups") to exist on **every**
non-root cgroup and be structurally absent **only** on the true root. This
is a property of the cgroup's real position in the kernel's actual
hierarchy — not of how cgroup-namespace virtualization happens to display
its path — so a namespace root that is secretly a real non-root cgroup
still has a real `cgroup.type` file underneath it and correctly fails the
check, regardless of what its own path displays as.

`_confirmed_true_cgroup_root(cgroup_dir)` requires **both** `memory.max`
and `cgroup.type` to be a confirmed ENOENT at the same directory. Either
one being merely ambiguous (permission error, any other I/O failure)
returns `False`, sending the caller to ordinary classification rather than
forgiving the absence — matching Section 12's instruction that unresolved
root identity classifies `UNKNOWN`, never invented.

`mount_root_norm == "/"` is kept only as a cheap pre-filter (skip the two
extra `open()` probes when this mapping isn't even namespace-displayed as
root) — never as proof by itself.

### ENOENT vs. inaccessible/error semantics

New `_probe_control_file_enoent(path) -> bool | None`: `True` only for a
genuine `FileNotFoundError`; `False` when the file opens successfully;
`None` for anything else (permission denied, `IsADirectoryError`, any
other `OSError`). This replaces `not os.path.exists(...)`, which collapses
all of those into the same `False`. Directly unit-tested, including a
real permission-denied case (`chmod 0o000` then attempt open, skipped if
running with privileges that bypass file permissions) and a
directory-where-a-file-was-expected case.

`_classify_cgroup_memory_level` (the general, non-root-exception per-level
reader) was **not modified** — it already caught `OSError` broadly and
returned `"unknown"`, which was already correct for ordinary (non-root)
levels; the root-only exception was the sole place granting ENOENT any
special meaning, and that's now gated through the new positive check.

### Namespace-root behavior

Both Section 13 scenarios now behave correctly and are directly tested:

- `/` displayed, `memory.max` genuinely finite there → read completely
  normally, regardless of the `cgroup.type` check (the memory.max-present
  branch short-circuits before `cgroup.type` is even consulted).
- `/` displayed, `memory.max` unresolved, `cgroup.type` present (proving
  this is a real non-root cgroup) → `"unknown"`, never forgiven as root.

The legitimate true-root case (both files confirmed ENOENT, matching this
host's own real shape) still correctly yields `"unlimited"` when the rest
of the hierarchy is genuinely unlimited — Section 14's requirement that
A2F14 not permanently force every normal host through the fallback.

## 4. Literal A2R14 replays (outside pytest)

Ran directly against the fixed module (script kept at
`/tmp/claude-1000/.../scratchpad/a2r14_replay.py`, not part of the repo):

```
REPLAY 1 (mixed evidence, cross-mapping): mapping A finite(1 GiB) dir,
mapping B genuinely unresolvable dir.
  OLD buggy rule would report: ('finite', 1073741824)
  NEW combined result:         ('unknown', 1073741824)
  PASS

REPLAY 2 (same mapping): finite leaf 1 GiB, malformed parent.
  OLD buggy rule would report: ('finite', 1073741824)
  NEW result:                  ('unknown', 1073741824)
  PASS

REPLAY 3 (slash-root ambiguity): '/' displayed on both proc/mountinfo
sides, memory.max unresolved, cgroup.type present (real non-root cgroup).
  OLD buggy rule would report: ('unlimited', None)
  NEW result:                  ('unknown', None)
  PASS
```

## 5. Test suite changes

**6 pre-existing tests were unsafe or fixture-incomplete, all fixed:**

- `test_evaluate_cgroup_mapping_mount_point_broken_leaf_finite` → renamed
  `..._retains_uncertainty`; asserted the literal forbidden collapse
  (`("finite", 192)` when the mount-point level is malformed); now
  asserts `("unknown", 192)`.
- `test_discover_cgroup_v2_availability_broken_applicable_first_valid_second`
  and `test_discover_cgroup_v2_availability_unrelated_then_broken_then_valid`
  → both renamed `..._retains_uncertainty`; asserted a genuinely
  unresolvable applicable mapping gets silently outvoted by a different,
  cleanly-resolved mapping; both now assert `("unknown", 134217728)`
  (uncertainty retained, known figure retained).
- `test_discover_cgroup_v2_availability_literal_a2r13_regression`,
  `test_discover_cgroup_v2_availability_applicable_subtree_and_root_mounts`,
  and `test_effective_available_memory_bytes_literal_a2r13_impact_regression`
  → not unsafe assertions, but **fixture-incomplete**: their intermediate
  ancestor levels above the leaf were never populated with memory files at
  all (a pre-A2F14 non-issue, since the old code silently discarded any
  resulting `"unknown"` the moment the leaf read cleanly). Fixed by giving
  every such ancestor an explicit `memory.max = "max"` reading, isolating
  each test's actual concern (mount/level selection) from the new,
  correct uncertainty-propagation behavior. Expected values unchanged.

Searched the complete file for the same "finite wins regardless of
unknown" assumption via `grep` across all `_evaluate_cgroup_mapping` /
`_discover_cgroup_v2_availability` call sites plus a full `-q` run to
surface every fixture gap; no other instances found.

**New tests added** (all under the `# --- ... A2F14 ...` section markers
in `tests/test_agent_backup.py`):

- `_probe_control_file_enoent` / `_confirmed_true_cgroup_root` direct unit
  tests (8 tests): confirmed-ENOENT, openable, permission-ambiguous (real
  `chmod 0o000`, skipped under privileges that bypass permissions),
  directory-not-file, and all four combinations of the two-file positive
  check.
- Section 13 namespace-root regressions (3 tests): finite-reads-normally,
  unresolved-is-unknown-not-unlimited, true-root-still-recognized-when-
  cgroup.type-also-absent.
- Section 16 combined tests (2 tests): finite mapping + ambiguous
  namespace-root mapping → uncertainty retained; finite mapping + proven
  true root → stays finite.
- Section 17 fail-closed host-merge examples (4 tests): looser known
  finite still capped by fallback; tighter known finite wins over
  fallback; known finite with missing host; known finite `0` honored
  (not mistaken for "no known figure").
- Section 8 monotonicity property (2 parametrized tests, 8 base evidence
  sets × [1 unknown-append case, 4 finite-append values] = 40 cases):
  `cap(S + UNKNOWN) <= cap(S)`, `cap(S + FINITE(x)) <= cap(S)`.
- Section 7 permutation invariance (1 parametrized test, 5 evidence sets,
  each checked across every permutation via `itertools.permutations`).

## 6. Focused receipt

```
tests/test_agent_backup.py
collected: 527
passed:    527
failed:    0
warnings:  1 (pre-existing, unrelated: zipfile duplicate-name warning
              in test_verify_rejects_duplicate_zip_member_name)
skips/xfailed: 0
runtime:   44.97s
native exit: 0
```

## 7. Full receipt

```
collected: 4090
passed:    4090
failed:    0
warnings:  1 (same pre-existing warning as above)
skips/xfailed: 0
runtime:   453.74s (0:07:33)
native exit: 0
```

One process-hygiene note unrelated to the fix itself: an earlier attempt
to capture the full suite's exit code via a `nohup ... & disown`
background launch produced a misleading task-notification claiming
"completed (exit code 0)" seconds after starting — that exit code
belonged to the launcher shell backgrounding itself, not to pytest, which
was still running (confirmed via `ps aux`, ~6s CPU time consumed of an
~8-minute run). The stray process was killed and the synchronous rerun
above is the actual, trustworthy receipt.

## 8. Remaining limitation

Root identity is recognized via `cgroup.type`'s documented absence, which
is real, kernel-guaranteed, and namespace-independent — but it only
answers "is *this* cgroup the true root," not "are there constraints on
some ancestor *outside* this process's own cgroup namespace that this
process cannot see at all." Cgroup namespacing is designed to make
everything above the namespace's own root structurally invisible to
processes inside it; no amount of reading `/proc/self/cgroup` or
`/proc/self/mountinfo` from inside the namespace can see past that
boundary. This is not a gap A2F14 introduces or could close — the module
already treats any full cgroup-discovery failure as `"unknown"` and caps
via the same conservative fallback, so an invisible ancestor constraint
degrades to "as conservative as total discovery failure," never to a
false "unlimited" claim.

## Pinned invariants

1. UNKNOWN evidence cannot be discarded by FINITE evidence.
2. Adding uncertainty can never widen Agent Backup's effective memory
   allowance.
3. Known finite constraints remain useful even when uncertainty exists;
   fallback handling must not erase a tighter known cap.
4. "/" is process-namespace-relative presentation, not proof of global
   cgroup-root identity.
5. Structural controller absence is recognized only from source-vetted
   positive evidence (`cgroup.type`'s documented absence, confirmed via
   genuine `ENOENT`); read/access/parser failures remain UNKNOWN.

## Duck

`skills/rubber-duck-debugging.md` SHA-256:
`d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be` —
unchanged, confirmed via `sha256sum`. Still captured, not shipped
baseline, provenance unknown.

The duck's request (stop naming cgroup states after emotions) is honored:
the new state carried by `"unknown"` is a plain retained number, not a
mood.

---
Return: **AGENT-BACKUP-RESTORE-A2R15**

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
