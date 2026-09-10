# AGENT-BACKUP-RESTORE-A2F12 Implementation Note (2026-09-10)

**Mission:** repair the two AGENT-BACKUP-RESTORE-A2R12 checkpoint blockers (process-relative
cgroup v2 memory accounting; canonical ZIP regular-file type bit) while preserving every
A2R12-cleared contract. No UI, no Restore, no unrelated cleanup, no commit, no push, no mirror.
Candidate returned for AGENT-BACKUP-RESTORE-A2R13.

## 0. Live Repository Gate

```
release  ~/lumina-release  branch main
  HEAD:      ee40259c980d3c6fcca56fc2f27dcefd3814c178  (== origin/main, 0 ahead/0 behind,
             confirmed via `git fetch origin --dry-run -v`: "= [up to date] main -> origin/main")
  status:    clean except the same untracked Agent Backup work files A2F1-A2F11 already left
             (core/agent_backup.py, tests/test_agent_backup.py, metadata/,
             AGENT_BACKUP_RESTORE_*.md) -- no hidden git-operation state (no MERGE_HEAD, no
             rebase-merge/rebase-apply/CHERRY_PICK_HEAD/BISECT_LOG present)
  hooks:     no .git/hooks/pre-push present

dev      ~/lumina  branch dev-local
  HEAD:      9b09542489210445b6b7fcc4ef2148e5f9eb64a8
  status:    1 modified tracked file (projects/lumina-dev/codebase.md) + 4 untracked
             skills/*.md files -- unrelated to Agent Backup, not touched by this pass
  ancestry:  release HEAD IS an ancestor of dev HEAD (confirmed via
             `git merge-base --is-ancestor ee40259c... HEAD`)
  remote:    release-local -> /home/bino/lumina-release (fetch), DISABLED (push)
  push.default: nothing
  pre-push hook: present, unconditionally exits 1 ("Push blocked: ~/lumina is a local-only
             development repository.")
```

Both HEADs matched the historical expectation given in the task exactly. Verified fresh, not
inherited from any prior session's report.

## 1. A2R12-cleared contracts -- preserved

Nothing in Sections 2-20 below touches: immutable filesystem-member bytes, SQLite
serialize/exact-byte validation, baseline single-snapshot authority, the working-set *formulas*
once N is known (only how N itself is discovered changes), no safety-defeating floor, build
amplification's conservative bounded model, the verifier's sequential-decompression model, the
host-safe operator-override clamp, strict JSON, the rest of canonical-ZIP enforcement
(compression/flags/extras/version/disk checks, ZIP64 ceiling), manifest/API truth, durability
warning separation, rollback/recovery integrity, 0600 published filesystem permissions,
namespace/malformed-input behavior, or the duck. All re-confirmed green in Sections 37-38 below.

---

## P0-1 (Sections 2-20): Process-relative cgroup v2 memory accounting

### Root cause

`_read_cgroup_v2_available_bytes()` (A2F11) read two hardcoded absolute paths,
`/sys/fs/cgroup/memory.max` and `/sys/fs/cgroup/memory.current`. That is only correct if the
current process is a *direct member of the cgroup2 mount's own root cgroup* -- true for almost
nothing on a real Linux desktop. Live source-vetting on this machine (this very Claude Code
process, running inside a sandboxed browser/app scope) confirmed the actual shape:

```
$ cat /proc/self/cgroup
0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-127092.scope
```

Five levels deep. The old code silently read the WRONG cgroup's memory.max/memory.current (the
mount root's, which on this host has no memory controller files at all -- see Section 36) --
functionally equivalent to budgeting against "whatever the landlord's whole building allows"
instead of the process's own apartment.

### Fix: three real kernel-provided sources of truth, composed

1. **`_read_process_cgroup_v2_path()`** -- parses `/proc/self/cgroup` for the line with
   hierarchy-ID `0` and an empty controller list (`0::<path>`, the unified/v2 hierarchy record
   per cgroups(7)). Explicitly ignores cgroup v1 lines (nonzero hierarchy-ID, non-empty
   controller list) that a hybrid host also reports on the same file. Returns `None` -- an
   explicit unknown, never a silent `"/"` substitution -- on a missing file or content with no
   parseable unified-hierarchy line.

2. **`_read_cgroup_v2_mount()`** -- parses `/proc/self/mountinfo` for the entry whose filesystem
   type is `cgroup2`, returning its `(mount_root, mount_point)` (fields 4 and 5, which precede
   any optional fields and the `-` separator token at a fixed position regardless of how many
   optional fields a line carries). Never assumes the mount point is `/sys/fs/cgroup` or that the
   mount root is `/` -- both vary under cgroup namespaces, bind/subtree mounts, and sandboxes.
   Field values are run through `_decode_mountinfo_field()`, a minimal decoder for the four octal
   escapes (`\040` space, `\011` tab, `\012` newline, `\134` backslash) the kernel's mountinfo
   writer applies to path fields -- not a general mount-table parser, per Section 16's own
   instruction not to build one.

3. **`_resolve_process_cgroup_dir()`** -- maps the logical path from (1) through the
   `(mount_root, mount_point)` pair from (2) onto the real filesystem directory. If
   `mount_root != "/"`, only the portion of the logical path *below* that root is appended to
   `mount_point` -- appending the full logical path unconditionally would double up the mount's
   own root segment (the literal Section 15 regression: logical `/user.slice/app.scope`, mount
   root `/user.slice`, mount point `/fake/cgroup` must resolve to `/fake/cgroup/app.scope`, never
   `/fake/cgroup/user.slice/app.scope`). Returns `None` when the logical path doesn't actually
   fall under the mount's own root at all (a namespace/subtree boundary -- Section 3's "return an
   explicit unknown result when resolution cannot be established"). The resolved directory is
   always checked to be at or beneath the mount point (Section 17) before being returned, even
   though real kernel-sourced `/proc` content never contains a `..`-escape attempt.

4. **`_read_memory_max_current(cgroup_dir)`** -- reads one level's `memory.max`/`memory.current`,
   clamped to `max(0, max - current)` (Section 10), returning `None` (contributes nothing) when
   `memory.max` is the literal string `"max"` or either file is missing/unreadable/unparseable.
   A broken/absent ancestor never propagates a failure past its own level (Section 9).

5. **`_read_cgroup_v2_available_bytes()`** -- resolves the leaf via (3), then walks every
   ancestor directory from that leaf up to (and including) the cgroup2 mount point itself via
   repeated `os.path.dirname`, collecting every level's finite `_read_memory_max_current()`
   result, and returns the **minimum** across all of them (Sections 6-8: a process is constrained
   by every enclosing cgroup, not merely its own leaf; the old root-only code would have reported
   whichever single level it happened to read, potentially far looser than the true binding
   constraint). Returns `None` only when no level anywhere in the walk reports a finite
   constraint -- identical in meaning to the pre-A2F12 "no cgroup constraint discoverable"
   contract `_effective_available_memory_bytes()` already relies on, so no caller above it needed
   to change.

The function signature and return contract (`int | None`) are unchanged, so
`_effective_available_memory_bytes()` and `_working_set_ceiling_bytes()` needed no edits at all
-- the repair is fully contained below them.

cgroup v1 (Section 18): not implemented, as in A2F11. Source-vetting found no current Lumina
deployment target that runs cgroup v1-only (this and every other checked host use the unified
v2 hierarchy exclusively, standard on any systemd default since ~2021); its absence degrades to
the same conservative `None` as "no cgroup2 mount found," never to "unconstrained."

### Literal regressions reproduced (Sections 7-9, 13, 15)

| Section | Scenario | Old behavior | New behavior |
|---|---|---|---|
| 7 | root available 3584, nested available 256 | would report whichever it read (3584, wrong) | 256 (correct: min across ancestors) |
| 8 | root unlimited, nested 384 MiB | `None` (root-only, saw no limit) | 384 MiB |
| 9 | root malformed, nested 192 | undefined/`None` | 192 (broken ancestor doesn't discard a good one) |
| 13 | host 8 GiB, process cgroup 384 MiB | N = 8 GiB (cgroup misread as root/unlimited) | N = 384 MiB, W = 192 MiB |
| 15 | mount root `/user.slice`, logical `/user.slice/app.scope` | n/a (not previously resolved at all) | resolves to `<mount>/app.scope`, not `<mount>/user.slice/app.scope` |

All five are now `tests/test_agent_backup.py` regression tests (Section 37 below), built against
a fake three-level `/proc/self/cgroup` + `/proc/self/mountinfo` + directory-tree fixture
(`_write_cgroup_v2_fixture`/`_write_memory_files`) per Section 14's instruction, not the old
single-path monkeypatch.

### Live self-adversarial replay (Section 36), against the REAL filesystem, unmocked

```
logical cgroup path: /user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-127092.scope
cgroup2 mount (root, mountpoint): ('/', '/sys/fs/cgroup')
resolved leaf dir: /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/app-com.anthropic.Claude-127092.scope
leaf exists on disk: True
```

Manually walked every ancestor from that leaf up to `/sys/fs/cgroup` and read each level's
`memory.max`/`memory.current` directly (bypassing the module) to cross-check the module's `None`
result:

```
.../app-com.anthropic.Claude-127092.scope -> max=max  current=3269320704
.../app.slice                             -> max=max  current=4692516864
.../user@1000.service                     -> max=max  current=4810403840
.../user-1000.slice                       -> max=max  current=12607041536
.../user.slice                            -> max=max  current=12611452928
/sys/fs/cgroup (mount root)               -> memory.max/memory.current: ENOENT (no memory
                                              controller files at the mount root on this host)
```

Every level genuinely unlimited or absent -- `_read_cgroup_v2_available_bytes()` correctly
returns `None` here, correctly falling through to the host `/proc/meminfo` figure
(`_effective_available_memory_bytes() == 4917383168`, ~4.7 GiB, matching `MemAvailable` read
independently). This is the resolution chain working correctly on real production hardware, not
merely against synthetic fixtures -- it resolved the process's actual nested app-scope directory,
confirmed it exists on disk, walked all six real ancestor levels, and handled the mount root's
missing files (Section 9's contract) without error. The synthetic fixture tests (Section 37)
separately cover the finite-constraint arithmetic this particular host happens not to exercise.

Unknown-unknown pass focused on `/proc/self/cgroup` parsing, mountinfo mapping, and the ancestor
walk (the seams named in Section 36): considered whether a cgroup namespace could make
`/proc/self/cgroup` report a path that is NOT a subpath of the mount's own root at all (e.g. a
containerized process whose cgroup namespace root differs from the host's view) --
`_resolve_process_cgroup_dir()` already returns `None` in that case (the `else: return None`
branch, tested directly) rather than guessing, which is the correct "explicit unknown" the
contract requires.

---

## P0-2 (Sections 21-30): Canonical ZIP regular-file type bit

### Root cause

`_V1_ZIP_EXTERNAL_ATTR = 0o644 << 16` encodes *permission bits only*. POSIX file-type and
permission bits are disjoint fields packed into the same `st_mode` word; `0o644` alone has zero
bits set in the type field, so `stat.S_ISREG(0o644)` is `False` -- the value is a "type-less
mode," not "a regular file with mode 644." Because the verifier's strict check was (and remains)
plain equality against this same constant, ANY external-attributes value that also happened to
carry permission bits `0o644` with no type bit -- including a mutated archive claiming to be a
type-less/unspecified-type member -- passed unnoticed, since it was byte-identical to what the
builder itself emitted.

### Fix

```python
_V1_ZIP_EXTERNAL_ATTR = (stat.S_IFREG | 0o644) << 16  # regular file, rw-r--r--
```

`stat.S_IFREG` is the standard-library regular-file type constant (`0o100000`). The corrected
high word is `0o100644` (`0x81a4`), and `stat.S_ISREG(0o100644) is True`. The low 16 bits (DOS
attributes under `create_system=3`/Unix) are untouched -- still `0`, so no directory/volume-label
DOS bit is accidentally introduced (Section 26).

This is the entire functional change. `_make_v1_zipinfo()` is the ONE canonical `ZipInfo`
construction every v1 member (ordinary payload, SQLite payload, baseline metadata, manifest.json)
goes through -- confirmed via `grep` that every `zf.writestr()` call in the builder passes through
it, so no per-member special-casing was needed (Section 23). The strict verifier's existing check,
`info.external_attr != _V1_ZIP_EXTERNAL_ATTR`, already enforces exact equality against the
canonical constant (Section 24: "prefer enforcing the exact canonical value already generated by
the builder") -- updating the one shared constant automatically tightens both builder and verifier
together, and automatically makes every builder-impossible type (type-less, symlink, directory,
FIFO, socket, character/block device) fail strict verification, since none of them can equal
`0o100644 << 16`. No separate verifier code path was needed for Section 25's entry-type matrix.

### Entry-type matrix (Section 25), extended

`test_verify_rejects_non_regular_file_external_attributes` (pre-existing, parametrized) gained
three cases the fix specifically closes:

- **`type-less-0644`** (`0o644 << 16`, no type bit at all) -- the literal historical bug, was
  this module's OWN prior canonical value, so this is the case that would previously have passed
  silently. Now correctly rejected.
- **`regular-wrong-perms-0600`** and **`regular-wrong-perms-0755`** (`(S_IFREG|perm) << 16` with
  a permission byte other than `0644`) -- confirm the verifier enforces the *exact* canonical
  value, not merely "any regular-file type bit with any permissions" (Section 24's explicit
  preference).
- **`char-device`** (`S_IFCHR`) -- rounds out the device-file coverage alongside the pre-existing
  block-device case.

New standalone tests: `test_v1_zip_external_attr_encodes_canonical_regular_file_type` (asserts
the bare constant decodes to `0o100644`, `S_ISREG` true, `S_IMODE` `0o644`, low word `0`,
independent of any archive) and an extension of
`test_make_v1_zipinfo_matches_source_vetted_real_builder_output` (Section 29's requested builder
corpus -- ASCII/unicode/zero-byte/binary/SQLite/manifest members via the ordinary `_build()` path)
to assert `S_ISREG`/`S_IMODE` per member, not just raw `external_attr` equality.

### Future Restore pin (Section 27)

Documented at the top of the canonical-attribute block in `core/agent_backup.py`: ZIP external
attributes establish that a v1 archive MEMBER is an ordinary file within the archive format. They
do not instruct a future Restore implementation to blindly apply mode `0644` to the restored
DESTINATION on disk -- destination permissions remain a separate, not-yet-designed policy. No
Restore implementation was added or changed in this pass.

---

## 31-34. Resource / SQLite / Rollback / Permissions regressions

Re-ran concise regressions in each area; no code in these areas changed, and no behavior change
was expected or observed beyond the cgroup accounting itself becoming more accurate for nested
processes:

- Resource: low-memory refusal, operator-override host-safe clamp, sequential verification
  residency, tiny-compressed/large-uncompressed prior, memory-discovery fallback -- all pass
  (Section 37).
- SQLite: owner DB stays WAL, snapshot isolation, serialize/deserialize exact-byte match, role
  validation, `quick_check`, resource refusal leaves the owner DB untouched -- all pass, no edits.
- Rollback/recovery: publication mismatch, immutable rollback, directory durability evidence,
  verified recovery preservation -- all pass, no edits.
- Permissions: filesystem-published backup/replacement/rollback/recovery artifacts remain `0600`
  -- an entirely separate contract from the ZIP member's own in-archive canonical mode
  (`0100644`), confirmed not conflated anywhere (Section 34).

## 35. Duck

```
skills/rubber-duck-debugging.md
sha256: d9e1dbbe15b9d04053b5d03690773cd1c9fe55c609934e68b2bc2d8329cbc2be  (exact match)
present:            yes
unchanged:          yes (hash matches exactly)
captured:           yes (recorded above)
not shipped baseline: yes -- explicitly listed in .gitignore (line 39:
                     "skills/rubber-duck-debugging.md"), confirmed via
                     `git check-ignore -v` and `git status --short --ignored`
                     (reports `!!`, never tracked, no git log entries at all)
provenance:          unknown (file mtime Jul 24 01:23, no git history -- consistent with its
                     deliberately-ignored status; not investigated further, matching A2F11's
                     own handling)
```

The duck has declined management again.

## 37. Focused Suite

```
tests/test_agent_backup.py
collected: 430  (was 399 before this pass; +31 new test cases)
passed:    430
failed:    0
warnings:  1 (pre-existing, unrelated -- test_verify_rejects_duplicate_zip_member_name's own
              deliberate duplicate-name construction; not touched by this pass)
skips/xfailed: 0
runtime:   46.40s
native exit: 0
```

New tests, cgroup (P0-1) -- `_decode_mountinfo_field` escape decoding;
`_read_process_cgroup_v2_path` (unified-hierarchy match, v1-line rejection, root-process `"0::/"`,
4-way malformed-content parametrize, missing file); `_read_cgroup_v2_mount` (cgroup2-entry match
with real-space mountinfo escaping round-tripped, no-cgroup2-entry, 3-way malformed-content
parametrize, missing file); `_resolve_process_cgroup_dir` (Section 15's mount-root regression,
root-mount-root nested path, process-at-mount-root-itself, logical-path-outside-mount-root ->
`None`, missing-file -> `None`); `_read_cgroup_v2_available_bytes` (Section 7 does-not-stop-at-
first-finite, Section 8 root-unlimited/nested-finite, Section 9 root-broken/nested-finite,
never-negative, basic max-minus-current, all-ancestors-unlimited -> `None`, no-ancestor-has-files
-> `None`, cgroup2-mount-absent -> `None`, three-level subtree-mounted hierarchy, Section 13's
literal end-to-end impact regression through `_effective_available_memory_bytes`/
`_working_set_ceiling_bytes`). New tests, ZIP (P0-2) --
`test_v1_zip_external_attr_encodes_canonical_regular_file_type`; three new parametrize cases on
`test_verify_rejects_non_regular_file_external_attributes` (`type-less-0644`,
`regular-wrong-perms-0600`, `regular-wrong-perms-0755`, plus `char-device`); `S_ISREG`/`S_IMODE`
assertions added to `test_make_v1_zipinfo_matches_source_vetted_real_builder_output`'s existing
builder-corpus loop. Rewritten (no longer possible against the new process-relative contract):
the four old `test_read_cgroup_v2_available_bytes_*` tests that monkeypatched
`_CGROUP_V2_MEMORY_MAX_PATH`/`_CGROUP_V2_MEMORY_CURRENT_PATH` directly -- those constants no
longer exist (replaced by `_PROC_SELF_CGROUP_PATH`/`_PROC_SELF_MOUNTINFO_PATH` plus the
filename-only `_CGROUP_V2_MEMORY_MAX_FILENAME`/`_CGROUP_V2_MEMORY_CURRENT_FILENAME`), since the
old constants encoded the exact root-only assumption this pass corrects.

## 38. Complete Release Suite

Full command: `python3 -m pytest -q` from the release repo root.

```
collected: 3993  (3962 A2F11 baseline + 31 new agent_backup tests from this pass)
passed:    3993
failed:    0
warnings:  1 (the same pre-existing, unrelated test_verify_rejects_duplicate_zip_member_name
              warning already noted in Section 37)
skips/xfailed: 0
runtime:   510.69s (0:08:30)
native exit: 0
```

Clean on the first attempt -- no repeat of A2F11's documented intermittent PySide6 full-suite
deadlock (unrelated to Agent Backup either way; see the memory note on that recurring issue).
Checkpoint requirement met: all collected tests pass, native exit 0.

## Remaining limitations

1. cgroup v1 memory accounting remains unimplemented -- no current Lumina deployment target
   relies on it (every checked host, including this one, runs the unified v2 hierarchy
   exclusively); a real future need should get its own separately-vetted pass rather than
   speculative untested parsing added here (Section 18, unchanged from A2F11's own position).
2. `_read_cgroup_v2_mount()` returns the FIRST cgroup2 entry found in mountinfo. On a host with
   multiple cgroup2 mounts (unusual outside deliberate multi-mount setups), this does not attempt
   to disambiguate which one governs the current process -- combined with
   `_resolve_process_cgroup_dir()`'s own "logical path must fall under this mount's root, else
   `None`" check, a mismatched first entry degrades to `None` (no finite cgroup signal) rather
   than a wrong finite one, so this remains conservative even in that unexercised case.
3. Both P0 fixes are narrowly scoped to the two named defects, per the mission's own "no
   unrelated cleanup" instruction -- no other A2R12-cleared contract was touched, reopened, or
   redesigned.

## Acceptance-sentence self-check

1. cgroup memory safety follows the current process into its actual cgroup hierarchy;
   root-cgroup files are not assumed authoritative. **CONFIRMED** -- Sections 2-20, live replay
   in Section 36 resolves this exact process's own nested scope on real hardware.
2. Every applicable finite cgroup2 ancestor may constrain the process; Agent Backup uses the
   tightest known remaining capacity. **CONFIRMED** -- Section 7/8/9 regressions, `min()` over
   every level in `_read_cgroup_v2_available_bytes()`.
3. Failure to resolve memory constraints never widens operation into an unbounded or
   operator-only allowance. **CONFIRMED** -- every discovery-failure path returns `None`, which
   `_effective_available_memory_bytes()` treats identically to "no cgroup present" (host figure or
   the finite `_FALLBACK_AVAILABLE_MEMORY_BYTES`, never unbounded); unchanged pre-existing
   contract, re-verified in Section 37's discovery-failure-matrix tests.
4. Every Agent Backup v1 ZIP member is canonically an ordinary regular file at the archive-format
   layer. **CONFIRMED** -- `_V1_ZIP_EXTERNAL_ATTR` decodes to `0o100644`/`S_ISREG` true, enforced
   identically by builder and verifier via the one shared constant, confirmed across the full
   builder corpus (Section 29 extension).
5. ZIP member mode semantics and restored destination permission policy are intentionally
   separate contracts. **CONFIRMED** -- Section 27 pin documented in-code; no Restore
   implementation exists yet to conflate them.

## STOP

No commit. No push. No mirror. No campaign cleanup. Candidate returned for
AGENT-BACKUP-RESTORE-A2R13.
