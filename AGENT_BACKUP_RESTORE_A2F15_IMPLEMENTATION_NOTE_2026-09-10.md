# AGENT-BACKUP-RESTORE-A2F15 — Implementation Note (2026-09-10)

Repairs the A2R15 P0 in `core/agent_backup.py`: missing `cgroup.type` and
`memory.max` files could classify a path as the true cgroup v2 root without
first proving that the path was a cgroup interface at all. A nonexistent,
ordinary, stale, or vanished directory therefore forged `UNLIMITED` cgroup
authority. Scope remained the root-authentication seam only. No UI, Restore,
unrelated cleanup, commit, push, or release-to-dev mirror was performed.

## 0. Live repository gate

- Release: `/home/bino/lumina-release`, branch `main`, HEAD and `origin/main`
  both `ee40259c980d3c6fcca56fc2f27dcefd3814c178`, ahead/behind `0/0`.
- Release residue before A2F15: the existing untracked Agent Backup source,
  tests, metadata, design note, and A2F through A2F14 implementation notes.
  No stash or additional worktree was present.
- Dev: `/home/bino/lumina`, branch `dev-local`, HEAD
  `9b09542489210445b6b7fcc4ef2148e5f9eb64a8`; the release head is its
  ancestor. Its pre-existing modified `projects/lumina-dev/codebase.md` and
  four untracked `skills/*.md` files were left untouched.
- Dev `release-local` fetch URL is `/home/bino/lumina-release`, push URL is
  `DISABLED`, `push.default=nothing`, and the executable pre-push hook exits 1.

## 1. A2R15 finding and literal pre-fix replay

`_confirmed_true_cgroup_root()` independently opened two child pathnames and
treated two `FileNotFoundError` results as positive root proof. It never opened
or authenticated the candidate directory itself and did not bind the two
observations to one same directory identity.

Direct pre-edit replay against the live candidate produced:

```text
nonexistent mountpoint:                 True
empty ordinary directory:              True
previously present then vanished:       True
FINITE(1 GiB) + vanished candidate:     ('finite', 1073741824)
live valid root:                        True
live valid root mapping:                ('unlimited', None)
```

The first three `True` results became forged `UNLIMITED` mapping evidence.
The fourth result showed A2F14's aggregation working exactly as designed on
bad input: because the vanished candidate had already been misclassified as
`UNLIMITED`, the fold had no uncertainty to retain.

## 2. Source-vetted positive authentication

Linux's current cgroup v2 documentation states that:

- the cgroup2 filesystem magic is `0x63677270` (`"cgrp"`);
- `cgroup.controllers` exists on all cgroups and may successfully read empty;
- `cgroup.type` exists on non-root cgroups, so its structural absence is a
  root discriminator only after the interface itself is authenticated.

Sources:

- <https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html>
- <https://github.com/torvalds/linux/blob/master/include/uapi/linux/magic.h>

The repair therefore requires both positive facts before absence has meaning:

1. `fstatfs(2)` on the already-open candidate directory descriptor reports
   `CGROUP2_SUPER_MAGIC`.
2. `cgroup.controllers` is opened, verified as a regular control node, and
   successfully read relative to that descriptor. Empty content is accepted.

The filesystem check is deliberately stronger than merely observing a file
named `cgroup.controllers`: an ordinary directory can forge that filename.

## 3. Descriptor-bound observation design

`_confirmed_true_cgroup_root(cgroup_dir)` now performs this sequence:

```text
os.open(candidate, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
  -> fstatfs(opened directory) == CGROUP2_SUPER_MAGIC
  -> openat/read cgroup.controllers through the same dir_fd
  -> openat cgroup.type through the same dir_fd; require ENOENT
  -> openat memory.max through the same dir_fd; require ENOENT
  -> close dir_fd in finally
```

All child opens are immediate names with `dir_fd`; none re-resolves the
candidate pathname. `O_NOFOLLOW` prevents a final symlink from substituting a
different directory/control node. Regular-file checks reject directories and
special-file impostors.

If the pathname is replaced after directory acquisition, later observations
remain bound to the acquired directory. If replacement occurs before
acquisition, the replacement must authenticate independently and an ordinary
directory fails closed.

## 4. Exact error semantics and lifetime

`_probe_control_file_enoent(dir_fd, filename)` returns:

- `True` only when the descriptor-relative `os.open()` itself raises genuine
  `FileNotFoundError`/`ENOENT`;
- `False` only when a present regular control node opens successfully;
- `None` for `EACCES`, `EPERM`, `EIO`, `ENOTDIR`, symlink loops, directories,
  special files, failed `fstat`, or any other ambiguous observation.

Root authentication accepts only:

```text
positive cgroup2 superblock
+ successful cgroup.controllers observation
+ cgroup.type ENOENT
+ memory.max ENOENT
```

Every other combination returns `False`, so `_evaluate_cgroup_mapping()` uses
ordinary classification and produces `UNKNOWN` when the controls cannot be
read. Directory and child descriptors close on success, root-compatible
ENOENT, permission failure, malformed control-node shape, read failure, and
unexpected exception. No control value grammar is imposed on
`cgroup.controllers`, because an empty controller list is valid.

## 5. Permanent regressions and self-attack

New or rewritten tests cover:

- live cgroup2 filesystem versus an ordinary temporary filesystem;
- successful empty `cgroup.controllers` observation;
- nonexistent directory-open failure;
- empty ordinary directory;
- ordinary directory with a forged-looking `cgroup.controllers` file;
- successful positive root authentication;
- present `cgroup.type` and present `memory.max` non-root cases;
- exact `ENOENT` versus `EACCES`, `EPERM`, `EIO`, and `ENOTDIR`;
- directory/special-shape control nodes;
- exactly one candidate-directory open and three descriptor-relative probes;
- pathname replacement after acquisition and ordinary replacement before the
  next acquisition;
- partial interface disappearance between authentication stages;
- directory-FD closure on success, permission, malformed node, and unexpected
  exception paths (success itself exercises both root-only ENOENTs);
- stale mountinfo mapping after the referenced directory vanishes;
- empty ordinary mapping root -> `UNKNOWN`;
- `FINITE(1 GiB) + vanished root` -> `UNKNOWN` carrying the known 1 GiB;
- positive true root -> `UNLIMITED`;
- namespace-visible `/` with finite `memory.max` -> `FINITE(128 MiB)`.

Self-attack found one additional causal weakness before the candidate gate:
`cgroup.controllers` success alone authenticates only a filename, because an
ordinary directory can contain a forged copy. The final implementation adds
descriptor-bound `fstatfs` cgroup2-magic authentication and a permanent forged
core-file regression.

The focused run also found six older multi-mount/snapshot tests whose synthetic
whole-hierarchy mount roots were empty ordinary directories. Those fixtures
had unintentionally depended on the A2R15 bug to forgive the mount-top level.
They now explicitly model a positively authenticated synthetic cgroup2 root;
their original multi-mount, idempotence, and snapshot assertions are unchanged.

## 6. Literal A2R15 replay after repair

Outside pytest, against the fixed module:

```text
nonexistent mountpoint:             ('unknown', None)
empty ordinary directory:          ('unknown', None)
previously present then vanished:   ('unknown', None)
FINITE(1 GiB) + vanished candidate: ('unknown', 1073741824)
live valid root:                    ('unlimited', None)
namespace-visible non-root:         ('finite', 134217728)
```

The A2F14 evidence algebra was not changed. Root-authentication uncertainty is
now correctly supplied to it, so uncertainty survives alongside any known
finite value and the existing effective-memory fallback remains monotonic.

## 7. Working-set arithmetic

Recomputed from the current implementation with N = 384 MiB:

```text
effective available N: 402653184 bytes
working-set ceiling W: 201326592 bytes
build payload limit:    22369621 bytes
verify payload limit:   67108864 bytes
```

No resource formula or constant changed.

## 8. Test receipts

### Targeted attack slice

```text
selected:    29
passed:      29
failed:      0
runtime:     0.88s
native exit: 0
```

After the first focused failure identified the six incomplete fixtures, their
exact corrected subset passed `6/6` in `0.74s`, native exit 0.

### Focused suite — attempt 1

```text
tests/test_agent_backup.py
collected:     543
passed:        537
failed:        6
warnings:      1 (intentional duplicate manifest.json ZIP-member test)
skips/xfailed: 0
runtime:       48.48s
native exit:   1
```

All six failures were the unsafe/incomplete synthetic true-root fixtures
described above. Production behavior was not weakened to satisfy them.

### Focused suite — attempt 2

```text
tests/test_agent_backup.py
collected:     543
passed:        543
failed:        0
warnings:      1 (same pre-existing intentional duplicate-name warning)
skips/xfailed: 0
runtime:       46.46s
native exit:   0
```

### Complete release suite — only attempt

```text
collected:     4106
passed:        4106
failed:        0
warnings:      1 (same pre-existing intentional duplicate-name warning)
skips/xfailed: 0
runtime:       507.30s (0:08:27)
native exit:   0
```

The focused and complete suites retain the established multi-mount, mixed
FINITE/UNKNOWN/UNLIMITED algebra, permutation/partition/idempotence/
monotonicity, mount-root mapping, ancestor walk, working-set, canonical ZIP,
SQLite exact-byte, immutable-member, strict-JSON, manifest, durability,
rollback/recovery, publication-mode, and duck regressions.

## 9. Duck and unchanged contracts

`skills/rubber-duck-debugging.md` remains present as a captured
`USER_OVERLAY + retained_artifact`, absent from the shipped baseline, with
unknown provenance. Its SHA-256 remains:

```text
d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be
```

Representative unchanged contracts remain covered by the focused suite:
canonical ZIP mode `0100644`/`S_ISREG`, archive publication mode `0600`, exact
serialized SQLite snapshots, immutable captured member bytes, duplicate-key
and NaN/Infinity rejection, returned-manifest/archive-manifest equality,
durability evidence outside the immutable manifest, and exact verified
rollback/recovery bytes.

## 10. Remaining limitations

- Positive filesystem authentication uses Linux `fstatfs(2)` through libc.
  When unavailable or non-Linux, root authentication fails closed; it does not
  invent `UNLIMITED` authority.
- The observation is intentionally time-bounded. A cgroup can change after the
  descriptor-bound probe; this function proves the one identity and interface
  observed during this call, not perpetual future state.
- Ordinary per-level `memory.max`/`memory.current` classification remains the
  pre-existing path-based ancestor walk. A2F15 changes only the special root
  exception that can erase an otherwise-unknown level; any ordinary read
  failure still becomes `UNKNOWN` through the existing algebra.

## Pinned invariants

1. Absence classifies an authenticated cgroup interface; it never
   authenticates one.
2. Root-authentication observations are bound to one opened directory
   identity.
3. Nonexistent, stale, vanished, replaced, or ordinary directories cannot
   forge `UNLIMITED` cgroup authority.
4. Namespace-visible `/` grants no root authority.
5. Any uncertainty created during root authentication feeds the existing
   monotonic fail-closed evidence algebra.

Candidate is ready for independent `AGENT-BACKUP-RESTORE-A2R16` review by
Tech. It is intentionally uncommitted, unpushed, and not mirrored.
