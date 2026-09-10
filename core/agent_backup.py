"""
core/agent_backup.py -- AGENT-BACKUP-RESTORE-A2F10

Eighth repair pass, built after AGENT-BACKUP-RESTORE-A2R8 independently
re-attacked the A2F7 candidate and found FOUR release-gating BLOCKERs, each
one attacking the SEAM between two individually-correct operations rather
than either operation alone:

  B1. Verification and immutable capture used different archive snapshots.
      A2F7's own _build_agent_backup_core called verify_agent_backup(tmp_path)
      -- which opens tmp_path BY NAME, reads it, verifies it, and closes its
      own handle internally -- and only THEN, in a separate later statement,
      reopened tmp_path BY NAME AGAIN to read built_data/built_sha256/
      built_size. A2R8 proved that gap is real: swap tmp_path's pathname to
      a DIFFERENT, independently verifier-valid archive B between those two
      opens, and B inherits A's already-completed PASS verdict -- built_data
      describes B, but nothing ever actually verified B itself.
  B2. Baseline archived payload and control/provenance evidence used
      different snapshots. _collect_metadata_members staged shipped_
      baseline_hashes.json's bytes into the archive via the universal
      capture boundary (one open, archived once), but _build_agent_backup_
      core separately called _load_baseline_hashes(baseline_hashes_path,
      ...), which reopened the LIVE baseline_hashes_path BY NAME a second
      time to parse it for the provenance decisions applied to every other
      USER_OVERLAY member. A2R8 proved a baseline swapped between those two
      opens lets an archived, Git-authenticated A coexist with a completely
      different B actually driving shipped_baseline_unmodified/modified
      classification.
  B3. Rollback trusted a mutable, unverified recovery pathname. A2F7's own
      fix for AGENT-BACKUP-RESTORE-A2R7's Section 26 finding preserved the
      prior destination via a bare os.link() to a predictable sibling path
      (dest_path + '.a2f7_prior_good.tmp') and, on a detected publication
      mismatch, restored it with a plain os.replace() -- no expected hash,
      no re-verification, no identity proof, and no post-restore
      observation. A2R8 found this trust model itself was the problem: a
      hardlink shares the SAME inode as whatever dest_path held before, so
      if that prior inode were mutated in place before rollback fired, the
      'recovery' copy would silently reflect the tampered bytes too.
  B4. Strict v1 ZIP verification accepted ZIP prefixes and concatenated
      archives. The existing trailing-bytes check (A2R7/Section 20) proved
      an archive's raw bytes exactly account for themselves AFTER its own
      End-Of-Central-Directory record, but never proved the archive's FIRST
      local file header starts at byte zero -- Python's zipfile module (and
      most real ZIP readers) locate the EOCD by searching backward and
      transparently adjust every entry's offset for a leading prefix, so a
      self-extracting stub, arbitrary leading garbage, or a second, complete
      ZIP archive concatenated in front of Lumina's own all passed strict
      verification undetected.

v1's answers, respectively (Sections 2-27 below hold the full mechanism for
each): (B1) _capture_and_verify_archive captures a freshly-built archive's
exact bytes via ONE open+read of its private staging pathname, and strictly
verifies THOSE bytes (io.BytesIO), never the pathname a second time --
tmp_path is deleted immediately afterward and never touched again for any
reason, including publication (see B1-adjacent redesign below); (B2)
_collect_metadata_members now captures shipped_baseline_hashes.json's bytes
via _capture_and_stage_physical_state_file (the same universal boundary,
returning the captured bytes alongside the staged path) and hands those
EXACT bytes to the new _parse_baseline_hashes for provenance classification
-- baseline_hashes_path itself is never reopened a second time inside a
build; the standalone, path-based _load_baseline_hashes remains available
for callers (and tests) that want to load a baseline outside a build, now
implemented as a thin wrapper that opens the path once, then delegates to
_parse_baseline_hashes; (B3) an existing destination is captured and
strictly verified via the SAME _capture_and_verify_archive boundary BEFORE
a build is ever allowed to touch it -- an unverifiable/uncaptureable prior
destination fails the whole build before publication rather than being
silently overwritten with no safe way back -- and a detected publication
mismatch restores the prior destination's immutable, already-verified bytes
via _publish_immutable_bytes (a fresh, randomly-named, single-use temp file,
never a predictable/reusable recovery pathname), itself content-verified by
re-observation before ever being described as restored; (B4)
_validate_zip_physical_structure independently walks the archive's own
central-directory-declared local file headers (using zipfile's own
prefix-adjusted header_offset values) and requires them to exactly tile
[0, cd_offset) with no gap, no overlap, and no unreferenced local record --
the first local header must start at byte 0, closing both the leading-prefix
and concatenated-archive attacks in one check -- plus per-member local/
central-directory agreement on filename, compression method, and size
(Section 22-23), scoped to exactly the structural subset Lumina's own
zipfile.ZipFile(..., ZIP_DEFLATED) builder emits, not general ZIP creativity.

Section 6's own broader question -- once an immutable built_data exists,
should publication stop depending on the original temporary ZIP inode at
all -- is answered yes: _publish_immutable_bytes writes the immutable bytes
being published (whether a fresh build or a rollback restoration) to a
brand-new, randomly-named, single-use temp file, fsyncs it, atomically
replaces dest_path, and independently re-observes the result by CONTENT
(never merely by inode identity -- A2F7's own Section 18 lesson) before
ever returning success. Forward publication and rollback restoration now
share this one evidence-producing mechanism rather than two independently-
written, potentially-divergent implementations of 'write bytes, verify
they landed.'

Every one of B1-B4 has a named defense below and a regression test in
test_agent_backup.py; every A2F7 defense this pass didn't need to touch is
preserved unchanged (see the module's git history / prior
AGENT_BACKUP_RESTORE_A2F*_IMPLEMENTATION_NOTE files for A2F through A2F7's
own reproductions and fixes). The paragraphs below, describing A2F7's own
two BLOCKERs, are kept as the historical record of that pass:

Seventh repair pass, built after AGENT-BACKUP-RESTORE-A2R7 independently
re-attacked the A2F6 candidate and found two release-gating issues, both
BLOCKERs:

  1. A2F6 wired its universal anti-hardlink/stable-capture boundary
     (_open_state_bearing/_stage_required_state_file) into every
     REQUIRED_AGENT_STATE collector, but THREE physical members --
     Project codebase.md, identity_trailers.json, shipped_baseline_
     hashes.json -- still routed through the OLD, weak _stage_plain_file
     (a single _open_verified + one unguarded _read_fd_fully): no
     st_nlink check, no stability proof, no retry. 'Optional' had only
     ever correctly meant one thing in this module -- a genuinely absent
     source is a valid, non-escalating outcome -- but these three call
     sites conflated it with 'a PRESENT file gets a weaker security
     boundary,' which was simply a real implementation gap, not a
     considered design choice (Section 2-6).
  2. Deeper than #1: A2F6's own fix for AGENT-BACKUP-RESTORE-A2R6's
     build-evidence-transfer finding bound receipt evidence to a file
     descriptor opened on the built archive BEFORE publication, but
     still DEFERRED the actual byte-read to whichever caller wanted
     receipt evidence, sometime after _build_agent_backup_core had
     already returned. A2R7 proved a live descriptor reflects its
     inode's CURRENT bytes at READ time, not a frozen snapshot from
     OPEN time -- a same-inode IN-PLACE rewrite (no os.replace(), no new
     inode, same dev/ino throughout) landing in that deferred window
     would have been silently read as if it were the verified build.
     Section 18's own words: 'same dev/inode != same archive bytes' --
     no amount of additional identity-checking machinery can make a
     MUTABLE descriptor immune to this; the deeper lesson (Section 10)
     is that a build receipt can only ever honestly attest bytes that
     were realized and hashed BEFORE publication, never a promise that a
     mutable, user-owned destination path stays that way afterward.

v1's answers, respectively: (1) the old, weak _stage_plain_file is
removed entirely; the renamed _stage_physical_state_file (formerly
_stage_required_state_file -- that name was itself part of the finding,
since it invited exactly the belief a non-'required' member could use
something weaker) is now the ONE physical-file capture boundary for
every filesystem-sourced ZIP payload, required or optional alike --
'optional' governs absence only, never the strength of the security
boundary applied to present bytes; (2) _BuiltArchiveResult now carries
already-realized `built_data`/`built_sha256`/`built_size` -- plain
Python values read and hashed EAGERLY, synchronously, immediately after
verification and before the publishing replace, never a descriptor or a
path read at any later moment -- plus a clearly-separate, explicitly
time-bounded `publication_observation` (Section 16-18) from one final,
content-based (not merely identity-based) re-read of the destination,
required to match the built bytes before a receipt is ever issued at
all; a detected mismatch attempts to restore whatever good archive
previously lived at dest_path rather than leaving it in a known-bad,
unattested state (Section 26).

Every one of those has a named defense below and a regression test in
test_agent_backup.py; each prior pass's own defenses are preserved (see
the module's git history / prior AGENT_BACKUP_RESTORE_A2F*_IMPLEMENTATION_
NOTE files for A2F through A2F6's own reproductions and fixes).

Four architectural statements this pass treats as load-bearing (AGENT-
BACKUP-RESTORE-A2F7, Section 32) -- future Restore/Backup work must
preserve all four:
  1. Every physical plain-file payload uses ONE strict capture boundary
     (_stage_physical_state_file), regardless of required/optional.
  2. A build receipt attests the immutable bytes produced and verified
     by that build invocation -- never bytes re-derived later from a
     mutable path or a deferred descriptor read.
  3. Publication-path observations are time-bounded observations, never
     a promise that a mutable filesystem path can never change again.
  4. Restore must reverify the actual archive bytes it is about to
     consume -- a past PASS receipt is not proof of present content.

Four further architectural statements THIS pass (AGENT-BACKUP-RESTORE-A2F8,
Section 34) treats as load-bearing, on top of the four above -- future
Restore/Backup work must preserve all eight:
  5. Verify the immutable captured archive, never a pathname that is later
     recaptured -- there is no verify(path) -> reopen(path) boundary
     anywhere in this module's build path.
  6. Any physical file used as both an archived payload and build-time
     control evidence (currently: shipped_baseline_hashes.json alone,
     Section 11's audited conclusion) is captured once and that exact
     capture is reused for both purposes -- never reopened by name a
     second time for the control decision.
  7. Rollback authority is immutable prior bytes/hash, never a mutable
     recovery pathname -- a predictable or reusable filename is never the
     thing this module trusts; the bytes and their sha256 are.
  8. Strict v1 ZIP verification accepts only the single, unambiguous
     physical structure Lumina's own builder emits -- no prefix, no
     concatenated archive, no unreferenced local record, no unexplained
     gap or overlap in the archive's own physical layout.

Ninth repair pass, built after AGENT-BACKUP-RESTORE-A2R9 independently
re-attacked the A2F8 candidate and found a single unifying theme across
four BLOCKERs and one HIGH/release-gating finding: A2F8 closed every SEAM
between two operations that each independently opened the SAME archive
path -- but left open a wider, more general version of the identical
mistake one level down, in how every ORDINARY MEMBER got from its source
into that archive in the first place:

  B1. Captured source bytes lost authority at mutable staging. Every
      physical member (a persona, prefs.json, a Project document, ...)
      was captured once via the stable-capture boundary, but only the
      resulting STAGED PATHNAME was kept -- the collector's own captured
      bytes were discarded immediately. _build_agent_backup_core then
      reopened that pathname by name for the manifest hash/size
      (_sha256_file), and reopened it a SECOND time, independently, for
      the ZIP payload (zipfile.write). A2R9 proved this live: substitute
      the staged file's content between those two reopens (same-inode
      overwrite, truncate, append, atomic replacement, symlink or
      hardlink replacement all worked), and the archive contains the
      SUBSTITUTED bytes under a manifest hash computed from the ORIGINAL
      bytes -- the exact "two representations of one truth briefly
      coexist" pattern, at the member level rather than the whole-archive
      level A2F8 already closed.
  B2. Recovery artifacts were reported without content verification.
      _preserve_rollback_failure_artifact (the absolute last resort, when
      both a forward publish and its own rollback have failed) wrote
      prior-archive bytes to a fresh path, fsynced it, and returned the
      bare path -- without ever proving the resulting path still held
      those exact bytes. A caller trusting "recovery preserved" had no
      actual evidence the preserved file was uncorrupted.
  B3. Duplicate/non-standard JSON authority semantics were accepted.
      Unrestricted json.loads(...) applies last-key-wins semantics to a
      repeated object key at any nesting depth, and parses the non-
      standard NaN/Infinity/-Infinity numeric tokens as a Python
      extension -- neither is acceptable for a format whose fields carry
      restore/rebind/executable authority.
  B4. Local ZIP headers were not fully bound to central-directory truth.
      Strict verification already agreed local and central records on
      filename, compression method, and compressed size (A2F7/A2F8), but
      never compared CRC-32, uncompressed size, or the full general-
      purpose flags word (only the single data-descriptor bit was
      checked) -- a local header could disagree with the central
      directory on any of those three fields and still verify valid=True.
  H1 (HIGH, release-gating for A2). The builder's zipfile.ZipFile(...)
      call left allowZip64 at its library default (True), while the
      strict verifier has never accepted ZIP64 (A2R7/Section 20/27) --
      meaning the builder could silently emit an archive (e.g. more than
      65535 members) that this same module's own verifier would then
      reject. Builder and verifier must speak one v1 ZIP64 contract, not
      two that can silently diverge.

v1's answers, respectively: (B1) the formerly-separate _stage_physical_
state_file / _capture_and_stage_physical_state_file are merged into ONE
function that ALWAYS returns (data, staged_path) -- every collector now
carries `data` (the immutable captured bytes) directly on its member
dict; _build_agent_backup_core's manifest hash/size and the ZIP payload
(zf.writestr, never zf.write) are both derived from that SAME `data`,
never from a fresh reopen of staged_path by name. staged_path itself
still exists (occasionally useful for diagnostics, and SQLite's online
backup API still requires a real destination file) but is transport/
cache only from this pass forward: nothing in this module ever rereads
it to determine payload bytes, a hash, a size, or manifest/ZIP content.
SQLite members get the identical treatment via a different mechanism
(sqlite3.Connection.serialize(), called on the live, just-checkpointed,
just-integrity-checked destination connection itself, before it is ever
closed -- source-vetted empirically, including the non-obvious
requirement to switch the connection off WAL journal mode first, since a
WAL-mode serialization otherwise fails to deserialize into a real
in-memory database afterward); (B2) _preserve_rollback_failure_artifact
now writes to a temp path, fsyncs it, atomically finalizes it, fsyncs the
containing directory (best-effort, honestly reported), RE-OBSERVES the
finalized artifact via the same universal stable-capture boundary
everything else in this module uses, requires its content hash to match
the immutable prior bytes it was supposed to preserve, and strictly
re-verifies it as a valid Agent Backup archive -- only then is
verified_at_preservation True; any failure along the way still reports
whatever bytes made it to disk (this function is only ever reached after
both a forward publish AND a rollback have already failed, so whatever
it manages to preserve, however unverified, is the last remaining copy
and is therefore kept, never deleted) but labeled UNVERIFIED, never
"recovery preserved" / known-good; (B3) one strict JSON decoder
(_strict_json_loads, via object_pairs_hook and parse_constant) rejects a
duplicate object key at any nesting depth and NaN/Infinity/-Infinity,
used everywhere this module parses authority-bearing JSON (manifest.json,
shipped_baseline_hashes.json, prefs.json); (B4)
_validate_zip_physical_structure now additionally requires local/central
agreement on CRC-32, uncompressed size, and the full general-purpose
flags word (not merely the data-descriptor bit) -- Section 24's
correction: there is no single flag_bits value every builder-emitted
member shares (a genuinely Unicode filename legitimately carries the
UTF-8 bit), so the contract is EQUALITY between local and central for
whatever each member's own records declare, not a hardcoded constant;
(H1) the builder now passes allowZip64=False explicitly and converts the
resulting zipfile.LargeZipFile into an honest AgentBackupError before any
publication is attempted -- v1 is deliberately, permanently non-ZIP64 on
both sides of this module, by construction, not by convention.

Every one of B1-B4/H1 has a named defense above/below and a regression
test in test_agent_backup.py; every A2F8 defense this pass didn't need to
touch is preserved unchanged (see the module's git history / prior
AGENT_BACKUP_RESTORE_A2F*_IMPLEMENTATION_NOTE files for A2F through
A2F8's own reproductions and fixes).

Six further architectural statements THIS pass (AGENT-BACKUP-RESTORE-A2F9)
treats as load-bearing, on top of the eight above -- future Restore/Backup
work must preserve all fourteen:
  9.  Once source bytes are securely captured (a physical file via
      _stage_physical_state_file, a SQLite snapshot via _snapshot_sqlite's
      own serialize() capture), no mutable staging pathname may redefine
      that member's content, hash, size, or ZIP payload ever again.
  10. SQLite role/schema/quick_check validation applies to the exact
      immutable snapshot bytes ultimately archived -- proven via a fresh
      in-memory deserialize() of those same bytes, never a separate
      reopen of a staging pathname.
  11. Recovery artifacts are only ever reported verified after content
      recapture, hash comparison, strict re-verification, AND durability
      observation -- never merely "written and fsynced."
  12. Manifest JSON (and every other authority-bearing JSON document this
      module parses) rejects a duplicate key at any nesting depth and any
      non-finite numeric constant.
  13. Strict ZIP verification agrees local and central directory records
      on every field the builder writes authoritatively -- filename,
      general-purpose flags, compression method, CRC-32, compressed
      size, and uncompressed size -- for the actual builder-emitted
      subset, not a hardcoded expectation.
  14. Agent Backup v1's builder and verifier share one explicit ZIP64
      contract (non-ZIP64, on both sides) -- the builder must never be
      able to silently emit a structure the same build's own verifier
      would reject.

Tenth repair pass, built after AGENT-BACKUP-RESTORE-A2R10 independently
re-attacked the A2F9 candidate and found the architecture itself finally
held (every previously-cleared invariant -- immutable member capture,
SQLite serialize()-based snapshot, baseline provenance, strict JSON,
rollback/recovery, publication-content observation -- survived a full
48-case staging-substitution attack matrix) -- but found three narrower,
still release-gating findings, all at the "format closure / truthful
durability / resource bound" layer rather than the architecture:

  B1 (BLOCKER). Strict verification accepted ZIP structures the v1
      builder cannot emit. Local/central AGREEMENT (A2F8/A2F9) is not
      the same claim as "the v1 builder could have produced this" --
      both records could agree on ZIP_STORED, a reserved/encryption/
      DEFLATE-option flag bit, or a non-empty extra field, none of which
      this module's own zipfile.ZipFile(..., ZIP_DEFLATED) builder ever
      writes. Separately, the builder's own physical-entry-count ceiling
      was enforced nowhere before construction: at exactly 65,535
      physical entries the builder itself still succeeds (zipfile's own
      file-count check is `> 65535`, not `>= 65535`), yet that exact
      count is indistinguishable from the ZIP64 "read the real count
      elsewhere" sentinel value and the strict verifier -- correctly --
      refuses it either way. A genuine v1 archive could therefore be
      built, fully paid for in time and temp-directory space, and only
      THEN discovered to be self-contradictory at its own verify step.
  H1 (HIGH). Main/rollback publication hid parent-directory durability
      failure. _publish_immutable_bytes always attempted a best-effort
      directory fsync after replacing dest_path, but its outcome was
      caught and silently discarded -- neither the forward-publication
      return value, the rollback-restoration code path's own narrated
      note, nor either public build entry point's return value ever
      revealed whether that fsync actually succeeded. Content
      publication and durability confirmation are different facts; the
      old code only ever reported the former.
  H2 (HIGH). Agent Backup had no aggregate resource bound. AGENT-BACKUP-
      RESTORE-A2R10 measured near-linear ~5x peak-RSS amplification over
      raw payload size (32 MiB payload -> +159.8 MiB RSS, 64 MiB -> +319.7
      MiB RSS; a 32 MiB prior backup plus a 32 MiB replacement build ->
      ~223.8 MiB added peak RSS) with no ceiling anywhere -- not during
      collection (every member's captured bytes simply accumulate,
      unbounded, across a build), not during verification of an
      existing/hostile archive (a 72,530-byte archive decompressing a
      single 64 MiB member, ~925:1, added ~60 MiB RSS during ordinary
      receipt verification with nothing to stop a far larger one).

v1's answers, respectively: (B1) strict verification now requires
membership in the source-vetted, CLOSED set of structures Lumina's own
builder actually emits (_ALLOWED_COMPRESSION_METHODS = {ZIP_DEFLATED},
_ALLOWED_GENERAL_PURPOSE_FLAGS = {0x0000, 0x0800}, zero-length local AND
central extra fields for every member) -- independently of, and in
addition to, the pre-existing local/central equality checks, so two
mutually-agreeing but builder-impossible records are rejected either
way; separately, _build_agent_backup_core now preflight-checks the
predicted physical entry count (every collected member, plus the one
always-generated manifest.json) against _MAX_PHYSICAL_ZIP_ENTRIES
(65534 -- one below the ambiguous ZIP64-sentinel boundary) BEFORE ever
opening a zipfile.ZipFile for writing, raising a clear AgentBackupError
instead of constructing (and only then discovering the invalidity of)
an archive this same build would immediately refuse to trust; (H1)
_publish_immutable_bytes now reports `directory_fsynced: bool` in its
return value (via the existing _best_effort_fsync_dir helper, previously
used only by the recovery-artifact path) -- content publication success
and directory-durability confirmation are two distinct, separately
reported facts everywhere this module surfaces either: the rollback
success-path's own narrated note, build_agent_backup_with_receipt's
`publication` key (automatic, since it already forwards the whole
publication_observation dict), and build_agent_backup's plain manifest
return value (via one honestly-labeled warning appended to a COPY of
the manifest's own `warnings` list, never mutating the immutable
captured manifest object itself) all now say so truthfully. A
directory-fsync failure never escalates into treating an otherwise
successful, content-verified publish as failed (A1's historical
best-effort durability model for this is preserved, not tightened into
an unrequested destructive rule) -- it is reported, not fabricated away
in either direction; (H2) one explicit, finite _AggregateBudget is
threaded through every collector and charged the instant each member's
bytes are captured (_stage_physical_state_file/_snapshot_sqlite alike --
no database exemption), never after collection has already finished;
its effective ceiling is derived from a documented default (2 GiB,
source-vetted against this codebase's own existing size-ceiling
conventions -- flight_recorder.py's 500 MiB DEFAULT_MAX_DB_BYTES,
sandbox.py's 512 MiB MAX_MEMORY_BYTES -- and against this machine's own
real current payload, under 20 MiB), an optional bounded environment
override (LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES, following this
codebase's existing LUMINA_* convention), and a downward-only clamp
against currently-available host memory (/proc/meminfo's MemAvailable)
using the SAME empirically-derived ~5x amplification factor (with a
conservative x6 margin above it) A2R10 measured, plus the prior
destination archive's own on-disk size (rollback authority may hold
that resident too) and a fixed overhead allowance. The identical
constant additionally bounds _verify_agent_backup_inner's own
decompression: every member's trusted, un-decompressed central-
directory-declared uncompressed size (never a hostile manifest's own
self-reported size) is checked, per-member AND in aggregate, BEFORE
zf.testzip() or any zf.read() can materialize a single decompressed
byte -- absolute limits, not a compression-ratio heuristic, so
legitimately-compressible ordinary state is never penalized purely for
compressing well.

Four further architectural statements THIS pass (AGENT-BACKUP-RESTORE-
A2F10) treats as load-bearing, on top of the fourteen above -- future
Restore/Backup work must preserve all eighteen:
  15. Strict v1 ZIP verification accepts only ZIP structures the
      Lumina v1 builder can actually emit -- membership in a closed,
      source-vetted set (compression method, general-purpose flags,
      empty extra fields), never merely internal local/central
      agreement on an otherwise-impossible value.
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

Every one of B1/H1/H2 has a named defense above and a regression test
in test_agent_backup.py; every A2F9 defense this pass didn't need to
touch is preserved unchanged (see the module's git history / prior
AGENT_BACKUP_RESTORE_A2F*_IMPLEMENTATION_NOTE files for A2F through
A2F9's own reproductions and fixes).

Architecture (unchanged since A2F -- still the right shape, only the trust
boundary and verifier keep getting stricter):

    discover canonical source
            |
    validate filesystem trust boundary (no symlinks, no directory-symlink
    escape, no hardlink alias to a forbidden identity) -- bound to the
    ACTUALLY-OPENED descriptor for plain files; for SQLite sources, bound
    to the file descriptor(s) NEWLY ACQUIRED BY THIS SPECIFIC CONNECTION
    via a before/after /proc/self/fd identity snapshot (see
    _connect_sqlite_with_bound_identity's docstring for the full A2R4
    reproduction this closes)
            |
    for EVERY physical, filesystem-sourced member -- a persona, a skill,
    a custom tool, tool_audit.log, a pending-action log, a Project
    document, prefs.json, and (A2F7, closing AGENT-BACKUP-RESTORE-A2R7's
    Section 2-6 finding) equally codebase.md, identity_trailers.json,
    shipped_baseline_hashes.json -- the shape check (regular file?
    symlink? FIFO?), the forbidden-identity check (credentials,
    ledger.db), AND (A2F6) a uniquely-linked check (st_nlink == 1 -- a
    physical file may never be a hardlink at all, regardless of what its
    other name points to) all happen in the SAME open+fstat call that
    captures the bytes (_open_state_bearing/_stage_physical_state_file)
    -- never a separate, earlier check with a gap to race (A2F5's fix
    for AGENT-BACKUP-RESTORE-A2R5's discovery-to-stage TOCTOU finding).
    'Required' vs 'optional' is entirely the calling collector's own
    business (whether a None return escalates) -- it is never a reason
    to use a weaker capture primitive; there is no lenient physical-file
    variant left in this module at all (A2F7 removes _stage_plain_file
    entirely). Anything other than a genuine, non-forbidden, uniquely-
    linked regular file fails the
    WHOLE backup, never merely excludes-with-a-warning, for these
    positions specifically -- Section 4/5's rule: a backup cannot
    truthfully claim complete recovery while deliberately omitting
    current durable state.
            |
    (A2F6) prove the bytes read through that descriptor are a single,
    STABLE, coherent version -- not merely that the descriptor itself was
    safe to open. A bracketed double-read (fstat -> read -> fstat ->
    seek(0) -> read -> fstat, requiring all three stat snapshots AND both
    reads to agree, see _read_stable_bytes_or_none) catches a concurrent
    same-inode writer even when it preserves size; one bounded retry from
    a fresh secure open recovers a genuine race, a persistently unstable
    source fails the whole backup rather than ever publishing a torn read
            |
    stage exact bytes into an isolated temp directory this process alone
    controls -- for SQLite stores, via sqlite3's own online backup API
    (a real point-in-time consistent snapshot, not a raw file copy after
    a checkpoint -- a checkpoint is not consistency, see _snapshot_sqlite)
            |
    hash + measure the STAGED bytes only
            |
    build the manifest from the staged bytes only
            |
    zip those exact staged bytes -- archive construction never reopens
    the original live source once staging has completed
            |
    close the zip, fsync it, verify it as a temp file -- verification
    independently re-derives SQLite integrity AND role-specific schema
    identity from the actual archived bytes, enforces Lumina's fixed v1
    policy for every recognized canonical/dynamic member regardless of
    what the archive's own manifest claims, enforces the full
    required-member floor, AND requires every physical member to belong
    to a recognized family at all, via a bijective logical_id<->
    archive_path derivation that rejects impossible nesting no real
    collector can produce (A2F5's closed-world model, A2F6 hardens the
    derivers themselves -- Section 16-24) -- v1 is closed-world, there is
    no unconstrained physical member type
            |
    (A2F6) bind build-time evidence to a descriptor opened on the built
    archive BEFORE the publishing replace -- never a later, separate,
    path-based reopen of dest_path (Section 15-18)
            |
    only on a clean verify: atomic os.replace() into the real destination
    (a failed build or failed verify never touches an existing good
    backup at dest_path), immediately followed by an independent
    post-replace identity check (lstat dest_path, compare against the
    inode just published) -- if dest_path was replaced again in the
    sliver of time since, this call fails closed rather than ever handing
    back evidence that could be mistaken for describing the replacement

Hard exclusions, enforced by construction:
  - credentials.json and ledger.db are never read, written, or
    referenced by content -- no function here imports core.secrets or
    core.idempotency. Their *identities* (device+inode, via os.stat()
    metadata only, never their bytes) are resolved once each and
    together form the forbidden_identities set (Section 9, A2F5 --
    widened from credentials-only after AGENT-BACKUP-RESTORE-A2R5 proved
    a ledger.db hardlink alias leaked its real rows the identical way a
    credentials hardlink previously could) checked against every
    candidate file -- for plain files, the check is bound to the
    descriptor actually opened for reading (no second path reopen); for
    SQLite sources, only the file descriptor(s) NEWLY ACQUIRED by this
    specific connection are ever consulted (see
    _connect_sqlite_with_bound_identity's docstring for the full A2R4
    reproduction this closes). For a REQUIRED, state-bearing member, a
    forbidden-identity match fails the WHOLE backup (_open_state_bearing,
    Section 10) rather than merely excluding the one file -- unlike the
    lenient _open_verified path, still used for genuinely optional
    members (identity_trailers.json, codebase.md), where a match
    continues to exclude just that file with a warning.
  - ledger.db's CONTENT is additionally never staged, snapshotted,
    hashed, or archived at all, under any archive_path -- REBUILDABLE_
    GENERATED (A1 Section C/L, amended): recorded only as a `regenerate`
    manifest entry with no archive_path/sha256/size at all, and the sole
    permitted regenerate entry's own state_class/restore_policy fields
    are value-checked, not just its logical_id's presence. The verifier
    enforces physical-payload absence by an ALLOWLIST of permitted
    archive namespaces plus an explicit ledger-name check, not by
    matching one specific expected bad path.
  - (A2F6, Section 1-3; A2F7, Section 2-6, extended to EVERY physical
    member, not only REQUIRED_AGENT_STATE ones) A physical, filesystem-
    sourced file may never be a hardlink at all (st_nlink == 1, checked
    on the opened descriptor's own fstat, alongside and independent of
    the forbidden_identities check above) -- this is not a wider
    blacklist, it is the recognition that a blacklist resolved once, up
    front, can never know about an inode that starts existing after that
    resolution (a real credentials/ledger.db rotation), so v1 closes the
    entire alias class instead: a physical file's identity may never be
    shared with any other name on the filesystem, regardless of what
    that other name currently points to or will ever point to. A2R7
    found this had only been wired into required positions, leaving
    codebase.md/identity_trailers.json/shipped_baseline_hashes.json on
    an older, weaker primitive (_stage_plain_file, now removed) --
    'optional' has only ever meant 'absence may be allowed,' never 'a
    present file gets a weaker boundary.' Source-vetted against this
    module's own house style before adoption -- see tools/file_edit.py's
    pre-existing, independently-arrived-at 'refusing to edit a hard-
    linked file (nlink=N)' policy, the same category of guarantee for
    the same underlying reason.

Baseline provenance (Section 1-4, A2F5's rewritten model -- see
_verify_baseline_against_git's own docstring for the full derivation):
baseline.git_commit is a PROVENANCE ANCHOR, not 'the commit currently
running' -- trust requires the anchor to be a real commit, an ancestor
of (or equal to) current HEAD, AND every baseline-owned path's blob to
match the declared hash at BOTH the anchor commit and current HEAD. This
survives a real release checkpoint (whose own commit necessarily
descends from whatever anchor it declares) and ordinary forward
development or a dev-local mirror, while still rejecting a stale
baseline the instant a later commit actually changes a file it
describes -- no git fixed point is required, unlike A2F4's exact-HEAD
design.

What this module does NOT claim: it cannot detect a secret a human
manually pasted into a persona, a skill, a custom tool, or any other
ordinary text file it is designed to archive verbatim. The truthful
guarantee is narrower and stated exactly that way in the backup receipt:
archive_policy_credentials_excluded, not "this archive contains no
secrets" -- and, separately (the build-evidence-vs-archive-assertion
split), build_credentials_boundary_verified can never be asserted True
through the public build_backup_receipt at all (A2F5, closing AGENT-
BACKUP-RESTORE-A2R5's public-API forgery finding) -- only build_agent_
backup_with_receipt, immediately after its own build has actually run
those live checks, can produce it, via a private, sentinel-guarded
_BuildSecurityEvidence object bound to the exact archive bytes it
describes; every other caller honestly reports
"not_provable_from_archive_alone" rather than trusting an archive's own
self-declared policy, or a caller's own say-so, as if either were
build-time proof. That receipt field, and every other receipt field, is
DERIVED from the verifier's own structured evidence (never a raw, unfiltered
manifest array) -- see build_backup_receipt.
"""
import ctypes
import errno
import hashlib
import io
import json
import os
import platform
import re
import sqlite3
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unicodedata
import warnings
import zipfile
from datetime import datetime, timezone

MANIFEST_FORMAT = "lumina-agent-backup"
MANIFEST_FORMAT_VERSION = {"major": 1, "minor": 0}

BASELINE_FORMAT = "lumina-shipped-baseline"
BASELINE_FORMAT_VERSION = {"major": 1, "minor": 0}
_BASELINE_CATEGORIES = ["personas", "assets/avatars", "assets/voices", "skills",
                         "tool_profiles", "projects/projectlist.md"]

_SKIP_DIR_NAMES = {"__pycache__", ".git"}
_VALID_STATE_CLASSES = {
    "REQUIRED_AGENT_STATE", "USER_OVERLAY", "REBUILDABLE_GENERATED",
    "REBIND_REQUIRED", "EXCLUDED",
}
_VALID_PORTABILITY = {
    "PORTABLE", "PORTABLE_WITH_REBIND", "SAME_MACHINE_ONLY", "REBUILD", "EXCLUDED",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_BACKUP_TIMEOUT_SECONDS = 30
_SQLITE_MAGIC = b"SQLite format 3\x00"

# ---------------------------------------------------------------------
# AGENT-BACKUP-RESTORE-A2F10 (HIGH H2): one explicit, finite aggregate
# uncompressed-resource budget, shared by BOTH sides of this module --
# the builder (aggregate bytes of every member's captured, immutable
# `data` held resident across one build) and the verifier (aggregate/
# per-member DECOMPRESSED bytes materialized by zf.testzip()/zf.read()
# while strictly verifying an archive, hostile or genuine). One number,
# one contract: AGENT-BACKUP-RESTORE-A2R10 measured near-linear ~5x
# peak-RSS amplification over raw payload size (32 MiB payload -> +159.8
# MiB RSS, 64 MiB -> +319.7 MiB RSS) with NO aggregate ceiling anywhere
# in either path -- this section makes that amplification bounded and
# explicit, not eliminated (eliminating it is future performance work,
# AGENT-BACKUP-SPARSE-LARGEFILE-01, explicitly out of scope here).
#
# Source-vetted default derivation (Section 21, "do not just invent
# 512 MiB because it sounds reasonable"): this machine's real
# DATA_DIR+BASE_DIR payload (lumina.db, flight_recorder.db, personas,
# avatars, voices, skills, tool_profiles, project docs -- everything
# this module actually archives) totals under 20 MiB today. This
# codebase already has two existing size-ceiling conventions in the
# same rough band: core/flight_recorder.py's own DEFAULT_MAX_DB_BYTES
# (500 MiB) and tools/sandbox.py's MAX_MEMORY_BYTES (512 MiB, a sandbox
# process's own address-space cap). 2 GiB sits comfortably above both
# of those existing conventions and roughly 100x above real current
# usage -- generous headroom for genuine growth (larger chat histories,
# more skills/personas, bigger voice/avatar assets) while still being a
# firm, finite v1 ceiling rather than an unbounded "however much RAM
# happens to be free" policy.
_DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
# Floor for an operator-supplied override ONLY (Section 28/A2F11 Section
# 16) -- sanity-bounds LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES itself so an
# absurd/degenerate value (0, negative, a single byte) is never honored
# as the CONFIGURED ceiling. This is NOT a safety floor applied to the
# host-derived working-set figures below -- AGENT-BACKUP-RESTORE-A2R11
# (BLOCKER, resource policy) found the previous design used an identical
# constant as a floor on the HOST-CLAMPED value too (`if safe_for_available
# < _MIN_...: safe_for_available = _MIN_...`), which silently defeated the
# whole point of computing a safe value on a genuinely tiny host (a 256
# MiB host could still be pushed to an estimated ~160 MiB peak against a
# 128 MiB safety target). A2F11 removes that use entirely: a host too
# small to safely support even one byte of new payload now refuses the
# build outright (Section 6 below) rather than being floored upward past
# what it can actually support.
_MIN_AGGREGATE_UNCOMPRESSED_BYTES = 16 * 1024 * 1024  # 16 MiB
# Ceiling for an operator-supplied override: refuse to honor an
# absurdly large value that would defeat the point of having a budget.
_MAX_AGGREGATE_UNCOMPRESSED_BYTES_CEILING = 16 * 1024 * 1024 * 1024  # 16 GiB
# Above A2R10's own measured ~5.00x empirical amplification ceiling for
# the BUILD pipeline specifically (capture + retained member bytes +
# zipfile's own internal compression buffers + the final built archive
# blob, all simultaneously resident -- Section 26: "source-vet an
# appropriate conservative factor above the observed ~5x"). Verification
# (decompression only) has its own, much smaller, separately measured
# factor below (_VERIFICATION_AMPLIFICATION_FACTOR) -- conflating the two
# was never correct (A2F11 Section 1/2/11), even though A2F10 happened to
# only use this one for both.
_RESOURCE_BUDGET_SAFETY_FACTOR = 6
# AGENT-BACKUP-RESTORE-A2F10's own empirical measurement (Section 20/34):
# a 64 MiB member's decompression during ordinary verification added
# ~60 MiB RSS (~0.94x, i.e. roughly "one output buffer's worth"), utterly
# unlike the build side's ~5-6x. 2x keeps comfortable headroom above that
# single measured data point for zlib's own internal decompressor state
# and multiple/aggregate members without pretending verification costs as
# much memory pressure as a full build does.
_VERIFICATION_AMPLIFICATION_FACTOR = 2
# Allowance for zipfile/CPython/library bookkeeping overhead beyond the
# payload x safety-factor estimate -- deliberately small, since the x6
# factor already covers the great majority of the measured overhead.
_RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES = 64 * 1024 * 1024  # 64 MiB
# Section 18 -- a small, bounded per-member allowance (manifest record,
# path strings, ZIP central/local metadata, Python object overhead) so a
# very large member COUNT is never treated as free merely because its
# aggregate payload is small (e.g. many zero-byte members). Applied only
# where member count is known BEFORE any decompression is attempted (the
# verifier's own trusted central-directory infolist) -- the build side
# does not know its eventual member count in advance, and is already
# separately bounded by _MAX_PHYSICAL_ZIP_ENTRIES (65,534 members x this
# same allowance is ~128 MiB, comfortably inside the existing fixed
# overhead reserve, so no separate build-side accounting is needed).
_RESOURCE_BUDGET_PER_MEMBER_STRUCTURAL_OVERHEAD_BYTES = 2048
# Never let the estimated peak working set exceed this fraction of
# effective available memory (Section 27 / A2F11 Section 7).
_RESOURCE_BUDGET_HOST_MEMORY_FRACTION = 0.5
# A2F11 Section 5 -- when NEITHER host (/proc/meminfo) NOR cgroup v2
# memory accounting can be discovered at all (non-Linux entirely, or a
# sandboxed/masked /proc with no cgroup v2 present), assume the exact
# reference low-memory scenario A2F11's own Section 6 worked its
# floor-removal example against (256 MiB effective available -> 128 MiB
# working-set ceiling -> a small, nonzero, genuinely conservative build
# payload allowance) rather than treating total discovery failure as
# license for an unbounded operation (the A2R11-flagged failure mode of
# the previous design, where discovery failure left the CONFIGURED
# ceiling -- up to 16 GiB via operator override -- in effect as a de
# facto host-safety allowance). Deliberately NOT this contract's own
# absolute matrix floor (128 MiB, Section 15) -- at THIS module's fixed
# 64 MiB overhead reserve, 128 MiB effective available yields a working-
# set ceiling of exactly 64 MiB, which is already fully consumed by
# fixed overhead alone (a legitimate, INTENDED hard-refuse tier for a
# host KNOWN to be that small, per Section 6: "a low-memory host is
# allowed to refuse backup"). Total discovery failure is a DIFFERENT
# situation -- it says nothing about whether the real host is tiny or
# merely running somewhere /proc is masked -- and defaulting it to the
# one scenario in this contract that ALWAYS refuses, unconditionally,
# on every non-Linux platform forever, would not be "conservative," it
# would be "silently non-functional," which Section 5 does not ask for.
_FALLBACK_AVAILABLE_MEMORY_BYTES = 256 * 1024 * 1024  # 256 MiB
# AGENT-BACKUP-RESTORE-A2F12 (P0-1 / A2R12): the fixed /sys/fs/cgroup/
# memory.* pair these replaced silently assumed the CURRENT PROCESS is
# always a direct member of the cgroup2 mount's ROOT cgroup. A2R12
# source-vetted this live and found the opposite is the common case --
# any desktop session, container, or sandboxed app scope nests the
# process several levels deep (e.g. /user.slice/user-1000.slice/
# user@1000.service/app.slice/app-<name>.scope). Resolving the process's
# OWN cgroup now goes through the two real kernel-provided sources of
# truth instead of a hardcoded path (Sections 3-5 below).
# AGENT-BACKUP-RESTORE-A2F15: neither a displayed "/" nor missing root-only
# files authenticates a cgroup interface.  Root recognition first opens the
# candidate directory once, confirms that exact descriptor belongs to the
# cgroup2 filesystem and can read its mandatory cgroup.controllers core file,
# and only then gives special meaning to descriptor-relative ENOENT results
# for cgroup.type and memory.max.  The Linux kernel documents the cgroup2
# superblock magic as 0x63677270 and cgroup.controllers as existing on every
# cgroup (its contents may legitimately be empty).  cgroup.type then provides
# the root discriminator: it exists on every non-root cgroup and is absent on
# the true root.  All observations remain bound to the one opened directory
# identity even if its pathname is replaced concurrently.
_CGROUP_V2_TYPE_FILENAME = "cgroup.type"
_CGROUP_V2_CONTROLLERS_FILENAME = "cgroup.controllers"
_CGROUP2_SUPER_MAGIC = 0x63677270
_CGROUP_V2_FSTYPE = "cgroup2"
_PROC_SELF_CGROUP_PATH = "/proc/self/cgroup"
_PROC_SELF_MOUNTINFO_PATH = "/proc/self/mountinfo"
_CGROUP_V2_MEMORY_MAX_FILENAME = "memory.max"
_CGROUP_V2_MEMORY_CURRENT_FILENAME = "memory.current"
_AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV = "LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES"


class _AggregateBudget:
    """AGENT-BACKUP-RESTORE-A2F10 (HIGH H2, Section 20-26): a finite
    ceiling on the aggregate uncompressed bytes this module will hold
    resident for the DURATION OF ONE BUILD -- charged incrementally, at
    the moment each member's bytes are captured (_stage_physical_state_
    file / _snapshot_sqlite), never after collection has already
    finished and every member's bytes are already resident (Section 22:
    'the budget must be enforced while collecting members, not after
    all data already occupies RAM'). SQLite members are charged the
    identical way as plain files (Section 23: 'no database exemption') --
    there is exactly one ledger, shared by every collector, via the same
    universal capture boundaries every other invariant in this module
    already goes through."""
    __slots__ = ("limit_bytes", "consumed_bytes")

    def __init__(self, limit_bytes: int):
        self.limit_bytes = limit_bytes
        self.consumed_bytes = 0

    def charge(self, nbytes: int, label: str) -> None:
        new_total = self.consumed_bytes + nbytes
        if new_total > self.limit_bytes:
            raise AgentBackupError(
                f"aggregate uncompressed backup payload would reach {new_total} byte(s) "
                f"after capturing {label} ({nbytes} byte(s)) -- exceeds this build's "
                f"resource budget of {self.limit_bytes} byte(s); refusing to retain "
                f"further state (any existing destination archive is left untouched, "
                f"since this fails before any publication step is ever reached)"
            )
        self.consumed_bytes = new_total


def _read_meminfo_available_bytes() -> "int | None":
    """Best-effort, Linux-only discovery of currently available physical
    memory (Section 27, 'prefer standard-library/Linux-safe discovery
    rather than a new dependency'). /proc/meminfo's own MemAvailable
    estimate is preferred over os.sysconf('SC_AVPHYS_PAGES') -- empirically
    confirmed on real hardware to track the much more conservative
    MemFree counter instead of the kernel's own reclaimable-cache-aware
    estimate. Never raises; returns None (a non-Linux platform, an
    unreadable /proc/meminfo, or one with no parseable MemAvailable
    line) so callers fall through to the OTHER available signal (cgroup)
    or the finite fallback below -- host discovery failure alone must
    never mean unbounded (A2F11 Section 3/5)."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _decode_mountinfo_field(raw: str) -> str:
    """Decodes the octal-escape set the kernel's mountinfo writer applies
    to the root and mount-point fields (proc(5)) -- a path containing a
    space, tab, newline, or literal backslash is written back as \\040,
    \\011, \\012, \\134 respectively. This is intentionally NOT a general
    mount-table parser (Section 16 warns against that) -- just the minimum
    correct decoder for these two path fields."""
    if "\\" not in raw:
        return raw
    out = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 4 <= n:
            digits = raw[i + 1:i + 4]
            if len(digits) == 3 and all(d in "01234567" for d in digits):
                out.append(chr(int(digits, 8)))
                i += 4
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_cgroup_v2_mounts() -> "list[tuple[str, str]]":
    """AGENT-BACKUP-RESTORE-A2F13 (P0, Section 2/4, replacing A2F12's
    _read_cgroup_v2_mount): reads /proc/self/mountinfo ONCE and returns
    EVERY cgroup2 mount record visible in this process's own mount
    namespace, as (mount_root, mount_point) pairs -- never just the
    first one found. A2R13 source-vetted a live host with more than one
    cgroup2 mount entry (a bind mount / duplicate view of the real
    hierarchy is common) and found the old single-mount reader picked
    whichever entry happened to sort first, with no regard for whether
    it actually applied to THIS process's own cgroup path -- silently
    discarding the real, applicable, tighter-constrained mount whenever
    it sorted second. mountinfo's fixed fields 1-6 always precede any
    optional fields and the bare '-' separator token, so their position
    (index 3 and 4) is stable regardless of how many optional fields a
    given line carries. Applicability filtering happens LATER, against
    this process's own logical cgroup path (Section 5) -- this function
    does no filtering of its own; it is purely 'every cgroup2 mount that
    exists,' so the caller can evaluate all of them rather than trusting
    mount order as authority. Returns an empty list on a missing/
    unreadable file, a line too short to contain the fixed fields, or no
    cgroup2 entry at all -- never raises."""
    try:
        with open(_PROC_SELF_MOUNTINFO_PATH, "r") as f:
            content = f.read()
    except OSError:
        return []
    mounts = []
    for line in content.splitlines():
        if not line:
            continue
        tokens = line.split()
        try:
            sep_index = tokens.index("-")
        except ValueError:
            continue
        if sep_index < 6 or len(tokens) < sep_index + 2:
            continue
        fstype = tokens[sep_index + 1]
        if fstype != _CGROUP_V2_FSTYPE:
            continue
        try:
            mount_root = _decode_mountinfo_field(tokens[3])
            mount_point = _decode_mountinfo_field(tokens[4])
        except (IndexError, ValueError):
            continue
        mounts.append((mount_root, mount_point))
    return mounts


def _read_process_cgroup_v2_path() -> "str | None":
    """AGENT-BACKUP-RESTORE-A2F12 (P0-1, Section 3): parses /proc/self/
    cgroup for THIS process's own cgroup v2 (unified hierarchy) record --
    the line with hierarchy-ID 0 and an empty controller list, `0::<path>`
    (cgroups(7)). A hybrid host also reports cgroup v1 lines (nonzero
    hierarchy-ID, a non-empty controller list, e.g. `11:cpu,cpuacct:/...`)
    -- those are deliberately ignored, never mistaken for the unified
    line (Section 18: this module understands v2 only). Returns None on a
    missing/unreadable file or content with no parseable unified-hierarchy
    line -- an explicit unknown, never a silent substitution of '/' for a
    record that was never actually found (Section 3). Never raises."""
    try:
        with open(_PROC_SELF_CGROUP_PATH, "r") as f:
            lines = f.readlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        hierarchy_id, controllers, path = parts
        if hierarchy_id == "0" and controllers == "" and path.startswith("/"):
            return path
    return None


def _resolve_cgroup_mapping(logical_path: str, mount_root: str, mount_point: str) -> "str | None":
    """AGENT-BACKUP-RESTORE-A2F13 (Section 5-6, replacing A2F12's
    _resolve_process_cgroup_dir, which only ever considered ONE,
    first-found mount): determines whether ONE cgroup2 mount record is
    applicable to this process's logical cgroup path, and if so,
    resolves the process's own leaf directory under THAT mount. A mount
    is applicable only when the logical path lies at or beneath the
    mount's own root, matched on whole path COMPONENTS (Section 5) --
    '/user.slice2' is never treated as beneath '/user.slice' via a naive
    string prefix (guarded below by an exact '== root' / 'startswith(root
    + "/")' check, never a bare .startswith(root)). If the mount's root
    is not the filesystem root, only the portion of the logical path
    BELOW that root is appended to the mount point -- appending the full
    logical path unconditionally would double up the mount's own root
    segment (Section 6's literal regression). Returns None when this
    mount is not applicable to `logical_path` at all -- a namespace/
    subtree boundary Section 3 requires an honest 'not applicable' for,
    never a guess. The resolved leaf is always verified to land at or
    beneath the mount point (Section 17) before being returned -- defends
    against a pathological '..'-bearing input being used to escape the
    mount, even though real kernel-sourced /proc content never contains
    one. Pure (no I/O) -- both inputs are already-captured snapshot
    values, so this can be called once per mount candidate without
    rereading anything."""
    norm_logical = os.path.normpath(logical_path)
    norm_root = os.path.normpath(mount_root)
    if norm_root == "/" or norm_root == ".":
        relative = norm_logical
    elif norm_logical == norm_root:
        relative = ""
    elif norm_logical.startswith(norm_root + "/"):
        relative = norm_logical[len(norm_root):]
    else:
        return None
    resolved = os.path.normpath(mount_point + relative)
    mount_point_norm = os.path.normpath(mount_point)
    if resolved != mount_point_norm and not resolved.startswith(mount_point_norm + os.sep):
        return None
    return resolved


def _resolve_process_cgroup_dir() -> "str | None":
    """Convenience single-answer resolution for THIS process's own
    cgroup, for simple/diagnostic callers that just want one directory
    for the common case where at most one discovered cgroup2 mount is
    actually applicable. Reads /proc/self/cgroup and /proc/self/
    mountinfo once each, then resolves against every discovered mount
    (Section 4) via _resolve_cgroup_mapping, returning the leaf of the
    first APPLICABLE one found. THE REAL AVAILABILITY COMPUTATION
    (_discover_cgroup_v2_availability) NEVER CALLS THIS -- it evaluates
    every applicable mapping itself (Section 8) rather than collapsing to
    a single answer the way this convenience wrapper does; trusting
    'first applicable' as if it were the only one is exactly the kind of
    mount-order authority Section 2 removes from the real resource-
    accounting path. Returns None when the process's cgroup path or the
    mount table can't be read, or no discovered mount is applicable at
    all."""
    logical_path = _read_process_cgroup_v2_path()
    if logical_path is None:
        return None
    for mount_root, mount_point in _read_cgroup_v2_mounts():
        leaf = _resolve_cgroup_mapping(logical_path, mount_root, mount_point)
        if leaf is not None:
            return leaf
    return None


def _classify_cgroup_memory_level(cgroup_dir: str) -> "tuple[str, int | None]":
    """AGENT-BACKUP-RESTORE-A2F13 (Section 9, replacing A2F12's
    _read_memory_max_current, which folded 'explicitly unlimited' and
    'unreadable/malformed' into the same None): reads memory.max/
    memory.current from ONE cgroup v2 directory and classifies this
    level into exactly one of three states, never collapsing them --
    ('finite', available_bytes) when memory.max is a parseable ceiling
    (available_bytes is never negative -- Section 10/17: when
    memory.current has already reached or passed memory.max, clamped to
    0 rather than going negative); ('unlimited', None) when memory.max is
    the literal 'max' -- this level explicitly contributes no ceiling; or
    ('unknown', None) when either file is missing, unreadable, or
    unparseable -- a level that could not be resolved must never be
    silently treated as proof of 'unlimited' (Section 10). The caller
    (_evaluate_cgroup_mapping) is what applies Section 9's 'one broken
    ancestor must not discard a known-good reading found at another
    level' rule -- this function's only job is keeping 'unknown' visibly
    distinct from both other states so that rule can be applied
    correctly. Never raises."""
    try:
        with open(os.path.join(cgroup_dir, _CGROUP_V2_MEMORY_MAX_FILENAME), "r") as f:
            raw_max = f.read().strip()
    except OSError:
        return ("unknown", None)
    if raw_max == "max":
        return ("unlimited", None)
    try:
        max_bytes = int(raw_max)
        with open(os.path.join(cgroup_dir, _CGROUP_V2_MEMORY_CURRENT_FILENAME), "r") as f:
            current_bytes = int(f.read().strip())
    except (OSError, ValueError):
        return ("unknown", None)
    return ("finite", max(0, max_bytes - current_bytes))


def _descriptor_is_cgroup2(dir_fd: int) -> bool:
    """Return whether one already-open directory is on cgroup v2.

    Linux exposes the filesystem type through fstatfs(2); querying the
    descriptor, rather than the candidate pathname, keeps this authentication
    bound to the same directory identity as every later root probe.  A roomy
    opaque result buffer avoids duplicating architecture-specific ``statfs``
    tail layout here -- only its ABI-stable first ``long`` (f_type) is read.
    Any unavailable syscall, error, or unexpected result fails closed.
    """
    if platform.system() != "Linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fstatfs = libc.fstatfs
        fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
        fstatfs.restype = ctypes.c_int
        result = ctypes.create_string_buffer(256)
        if fstatfs(dir_fd, ctypes.byref(result)) != 0:
            return False
        return ctypes.c_ulong.from_buffer(result).value == _CGROUP2_SUPER_MAGIC
    except Exception:
        return False


def _open_control_file_at(dir_fd: int, filename: str) -> "int | None":
    """Open one immediate control file relative to ``dir_fd``.

    Symlinks, directories, and special files are rejected: real cgroup core
    and controller files present as regular files, while following a forged
    link would break the one-directory-identity boundary.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(filename, flags, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _observe_control_file_at(dir_fd: int, filename: str) -> bool:
    """Positively observe a descriptor-relative control file.

    A successful zero-byte read is valid -- cgroup.controllers may be empty.
    Open, type-check, and read failures are all uncertainty, never authority.
    """
    fd = None
    try:
        fd = _open_control_file_at(dir_fd, filename)
        if fd is None:
            return False
        os.read(fd, 4096)
        return True
    except Exception:
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _probe_control_file_enoent(dir_fd: int, filename: str) -> "bool | None":
    """Classify one control file relative to an authenticated directory.

    Returns True only for genuine ENOENT; False for a successfully opened
    regular control file; and None for EACCES, EPERM, EIO, ENOTDIR, symlinks,
    special files, or any other ambiguous outcome.  The caller must establish
    the cgroup interface before absence has any structural meaning.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(filename, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return None
    try:
        try:
            return False if stat.S_ISREG(os.fstat(fd).st_mode) else None
        except Exception:
            return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _confirmed_true_cgroup_root(cgroup_dir: str) -> bool:
    """Authenticate and classify the true cgroup v2 root, fail closed.

    Absence may classify a positively authenticated cgroup interface; it
    never authenticates one.  Open the candidate directory exactly once,
    prove that descriptor is a live cgroup2 filesystem with an observable
    mandatory core file, then require genuine descriptor-relative ENOENT for
    both root-only absences (cgroup.type and memory.max).  Path replacement
    after the directory open cannot mix identities.  Never raises.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        dir_fd = os.open(cgroup_dir, flags)
    except (OSError, TypeError, ValueError):
        return False
    try:
        try:
            if not _descriptor_is_cgroup2(dir_fd):
                return False
            if not _observe_control_file_at(dir_fd, _CGROUP_V2_CONTROLLERS_FILENAME):
                return False
            if _probe_control_file_enoent(dir_fd, _CGROUP_V2_TYPE_FILENAME) is not True:
                return False
            return _probe_control_file_enoent(dir_fd, _CGROUP_V2_MEMORY_MAX_FILENAME) is True
        except Exception:
            return False
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _finalize_cgroup_states(results: "list[tuple[str, int | None]]") -> "tuple[str, int | None]":
    """AGENT-BACKUP-RESTORE-A2F14 (P0-1, Section 2-8): the ONE monotonic
    combination rule shared by both accumulation sites in this module --
    per-level readings within one mapping's ancestor walk (Section 5), and
    per-mapping verdicts across every applicable cgroup2 mount (Section
    6). Replaces A2F13's own 'if finite_levels: return finite' /
    'if finite_values: return finite' ordering, which silently let a
    known-good finite reading discard a genuinely unresolved reading
    found elsewhere in the SAME walk or fold -- the exact A2R14 P0: an
    unreadable applicable constraint may conceal an arbitrarily tighter
    real limit (512 MiB, 128 MiB, 0...) and can never be discarded merely
    because some OTHER level/mapping happened to read cleanly.

    Never loses either fact: the tightest successfully-observed finite
    figure across every reading (min() over the whole set -- A2F12/13's
    own pre-existing tightest-wins contract, unchanged) survives into the
    result even when returning 'unknown', and the presence of ANY
    unresolved reading survives (any() over the whole set) even when a
    known finite figure also exists. 'unknown' with a non-None value is
    thus now a real, representable state -- Section 3's 'known finite
    constraint exists AND some applicable authority remains unresolved.'

    Built from min()/any() over the WHOLE set rather than a pairwise
    left-to-right fold, so this is inherently order-independent (Section
    7): permuting `results` can never change the outcome. An empty list
    (no readings applied at all -- e.g. a one-level walk whose only level
    was the confirmed true root, contributing nothing) correctly reduces
    to the identity, ('unlimited', None); callers with a genuinely empty
    DISCOVERY (no applicable mount/level found at all, a different
    situation from 'every applicable one resolved to no ceiling') are
    responsible for reporting that as 'unknown' themselves, since this
    function only ever combines readings that actually applied.

    Collects a candidate value from EVERY reading that carries one,
    regardless of its own state tag -- never only from readings tagged
    'finite'. This function is used recursively (the cross-mapping fold
    in _discover_cgroup_v2_availability combines results that are
    themselves already _finalize_cgroup_states output from each
    mapping's own ancestor-walk fold), and an inner 'unknown' result can
    itself carry a retained known-finite value (Section 3) -- filtering
    on state=='finite' alone would silently re-introduce the exact bug
    this function exists to fix, one recursion level up. Never raises."""
    finite_values = [v for _, v in results if v is not None]
    tightest = min(finite_values) if finite_values else None
    if any(s == "unknown" for s, _ in results):
        return ("unknown", tightest)
    if tightest is not None:
        return ("finite", tightest)
    return ("unlimited", None)


def _evaluate_cgroup_mapping(leaf_dir: str, mount_root: str, mount_point: str) -> "tuple[str, int | None]":
    """AGENT-BACKUP-RESTORE-A2F14 (repairing A2R14 P0-1/P0-2, replacing
    A2F13's own _evaluate_cgroup_mapping): walks every ancestor level from
    `leaf_dir` up to (and including) `mount_point` for ONE resolved
    (mount, process-leaf) candidate mapping -- the walk's boundary is
    always THIS mapping's own mount point, never a different candidate's
    (Section 7: resolved leaf, mount boundary, and ancestor walk all
    derive from the same captured mountinfo record) -- collects every
    level's own tri-state reading, and combines them via
    _finalize_cgroup_states's monotonic rule (P0-1: a level that could not
    be resolved can no longer be silently discarded just because another
    level in the same walk read a finite ceiling cleanly).

    The true, whole-system ROOT cgroup -- reached exactly when this walk's
    current level IS the mount point -- structurally has NO memory.max
    file at all (cgroups-v2.rst: resource control files simply do not
    exist on the root cgroup, since it has no concept of its own limit).
    That absence is not a broken or unreadable level; it is the kernel's
    own documented shape, and must never poison an otherwise
    fully-resolved, all-unlimited mapping into 'unknown' (Section 14).
    Recognizing it is now (P0-2) _confirmed_true_cgroup_root's job, never
    a bare `mount_root == "/"` pathname check -- `mount_root_norm == "/"`
    is kept only as a cheap pre-filter to skip the extra I/O probes when
    this mapping isn't even namespace-displayed as root, never as proof
    by itself (Section 9-13). A candidate that fails the confirmed-root
    check (ambiguous evidence, or a real non-root cgroup hiding behind a
    namespace-displayed '/') falls straight through to ordinary
    classification of that level, exactly like any other level. Never
    raises."""
    mount_root_norm = os.path.normpath(mount_root)
    mount_point_norm = os.path.normpath(mount_point)
    current_dir = os.path.normpath(leaf_dir)
    level_results = []
    # `leaf_dir` is already verified (by _resolve_cgroup_mapping) to sit at or
    # beneath mount_point_norm, so this walk never needs to step above it.
    while True:
        at_mount_top = current_dir == mount_point_norm
        if (
            at_mount_top
            and mount_root_norm == "/"
            and _confirmed_true_cgroup_root(current_dir)
        ):
            pass  # the real root cgroup never has memory.max -- not a failure, contributes nothing
        else:
            level_results.append(_classify_cgroup_memory_level(current_dir))
        if at_mount_top:
            break
        parent_dir = os.path.dirname(current_dir)
        if parent_dir == current_dir:
            break  # reached the filesystem root without matching the mount point
        current_dir = parent_dir
    return _finalize_cgroup_states(level_results)


def _discover_cgroup_v2_availability() -> "tuple[str, int | None]":
    """AGENT-BACKUP-RESTORE-A2F14 (repairing A2R14 P0-1, replacing
    A2F13's own _discover_cgroup_v2_availability): the ONE cgroup v2
    discovery entry point _effective_available_memory_bytes relies on.
    Reads this process's logical cgroup path (Section 3) and the full
    mount table (Section 4) EXACTLY ONCE EACH -- one snapshot of each
    source for the whole operation (Section 15), so a caller can never
    combine a mount resolved against one /proc/self/mountinfo generation
    with an ancestor walk against another. Evaluates EVERY cgroup2 mount
    that is actually applicable to the process's own path (Section 5/8)
    -- mount-table ORDER is never treated as authority.

    Combines every applicable mapping's own verdict via
    _finalize_cgroup_states's monotonic rule (P0-1) rather than A2F13's
    'if finite_values: return finite' ordering, which silently let one
    applicable mapping's clean finite reading discard a DIFFERENT
    applicable mapping's genuine unresolved reading (the literal A2R14
    cross-mapping P0). Returns a tri-state result that keeps three
    genuinely different situations visibly distinct (Section 9), so a
    caller can react to each correctly:
      ('finite', n)       -- every applicable mapping resolved, and at
                              least one reported a finite ceiling; n is
                              the MINIMUM finite figure across all of
                              them (Section 8/13: tightest valid
                              observation wins).
      ('unlimited', None) -- process cgroup resolved successfully under
                              at least one applicable mount, and EVERY
                              applicable mapping explicitly resolved with
                              no finite ceiling at any level (Section 11).
      ('unknown', n)      -- resolution failed outright (no logical path,
                              no cgroup2 mounts, or no mount applicable to
                              this process's path at all: n is None), or
                              at least one applicable mapping could not be
                              fully resolved (Section 9/10) -- this must
                              NEVER be treated as 'unlimited' by the
                              caller. `n` carries the tightest finite
                              figure some OTHER applicable mapping still
                              managed to observe, if any (Section 3/6) --
                              never silently dropped just because this
                              result is 'unknown' overall.
    Never raises."""
    logical_path = _read_process_cgroup_v2_path()
    mounts = _read_cgroup_v2_mounts()
    if logical_path is None or not mounts:
        return ("unknown", None)
    mapping_results = []
    for mount_root, mount_point in mounts:
        leaf = _resolve_cgroup_mapping(logical_path, mount_root, mount_point)
        if leaf is None:
            continue  # this mount is not applicable to this process's path
        mapping_results.append(_evaluate_cgroup_mapping(leaf, mount_root, mount_point))
    if not mapping_results:
        return ("unknown", None)  # cgroup2 mounts exist, but none applied
    return _finalize_cgroup_states(mapping_results)


def _effective_available_memory_bytes() -> int:
    """AGENT-BACKUP-RESTORE-A2F14 (Section 4/17, repairing A2R14 P0-1's
    downstream half): the ONE available-memory figure the rest of this
    module's resource contract is built on.
      - cgroup FINITE(n): N = the minimum of every finite signal (host
        included when known -- Section 8/17/21, unchanged from
        A2F11/A2F12/A2F13).
      - cgroup UNLIMITED: cgroup contributes no ceiling at all; N = host
        MemAvailable when host is known (Section 11), else the finite
        fallback.
      - cgroup UNKNOWN: uncertainty must never widen the allowance to
        host-only (the literal A2R13 P0, still preserved). N is now the
        minimum across every figure this pass actually has in hand: the
        conservative fallback, host (when known), AND a tighter known
        finite cgroup figure that coexisted with the uncertainty, when
        _discover_cgroup_v2_availability's own 'unknown' result carried
        one (Section 4/17 -- the A2R14 repair: 'unknown' used to always
        discard any finite figure it was paired with, which is exactly
        the case where honoring the tighter known number matters most).
        A known finite figure can only ever narrow this result, never
        widen it past what the fallback/host alone would have given.
    Total host-discovery failure alone (non-Linux, masked /proc) still
    must never be treated as license for an effectively unbounded
    operation (the exact failure mode AGENT-BACKUP-RESTORE-A2R11 flagged
    in the prior design). Always finite; never raises."""
    host = _read_meminfo_available_bytes()
    cgroup_state, cgroup_value = _discover_cgroup_v2_availability()
    if cgroup_state == "finite":
        return min(v for v in (host, cgroup_value) if v is not None)
    if cgroup_state == "unlimited":
        return host if host is not None else _FALLBACK_AVAILABLE_MEMORY_BYTES
    # cgroup_state == "unknown": fail closed -- never fall open to host-only,
    # but a tighter known finite figure (cgroup_value) must still narrow
    # this result rather than being discarded merely because some OTHER
    # applicable level/mapping was unresolved.
    candidates = [_FALLBACK_AVAILABLE_MEMORY_BYTES]
    if host is not None:
        candidates.append(host)
    if cgroup_value is not None:
        candidates.append(cgroup_value)
    return min(candidates)


def _working_set_ceiling_bytes() -> int:
    """AGENT-BACKUP-RESTORE-A2F11 (Section 2): the maximum memory
    pressure Agent Backup intentionally budgets ITSELF to use for one
    operation -- always finite (Section 3: unknown host memory never
    means unbounded), always a conservative fraction of effective
    available memory (Section 7: preserves A2F10's own ~50% target
    unless disproven, which this pass's own source-vetting did not
    disprove). Every PHASE limit below (build payload, verification
    payload/decompression) derives FROM this one figure -- this
    function itself is never a payload or decompression limit, only the
    working-set contract the phase limits are derived FROM (Section 2's
    explicit 'never the reverse')."""
    return int(_effective_available_memory_bytes() * _RESOURCE_BUDGET_HOST_MEMORY_FRACTION)


def _resolve_configured_payload_budget() -> int:
    """The operator-configurable half of the v1 resource contract
    (Section 28) -- LUMINA_AGENT_BACKUP_MAX_PAYLOAD_BYTES follows this
    codebase's existing LUMINA_* environment-override convention (see
    config.py's own LUMINA_DATA_DIR). An absent, non-integer, or
    out-of-[min,max]-range override is ignored in its entirety (never
    partially applied) and falls back to the documented default --
    there is no path here for an arbitrary, unvalidated integer to take
    effect (Section 28). This value may only ever CONSTRAIN the
    host-safe working-set contract further (Section 16: 'effective =
    min(configured, host-safe)') -- it never expands past what
    _working_set_ceiling_bytes derives, in either the build-payload or
    verification-payload deriver below."""
    raw = os.environ.get(_AGENT_BACKUP_MAX_PAYLOAD_BYTES_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = None
        if value is not None and (
                _MIN_AGGREGATE_UNCOMPRESSED_BYTES <= value <= _MAX_AGGREGATE_UNCOMPRESSED_BYTES_CEILING):
            return value
    return _DEFAULT_MAX_AGGREGATE_UNCOMPRESSED_BYTES


def _determine_aggregate_payload_budget(prior_backup_bytes: int = 0) -> int:
    """AGENT-BACKUP-RESTORE-A2F11 (Section 8-9, repairing the A2R11 P0
    resource-policy blocker): derives the effective aggregate
    uncompressed-PAYLOAD budget for one build's own NEW members, as a
    PHASE LIMIT derived from the host-safe working-set ceiling -- never
    the reverse. `available_for_new_build` is what's left of this
    build's working-set ceiling after reserving fixed
    zipfile/CPython/manifest overhead and whatever the existing
    destination archive's own on-disk (compressed) bytes retain resident
    during collection (Section 24: rollback authority may hold that
    whole prior archive resident while a fresh build is collected).
    `available_for_new_build <= 0` means this host cannot safely support
    ANY new payload at all under this build's own overhead alone --
    Section 6's explicit instruction is to refuse outright here, never
    to floor the result back up to some nonzero minimum (the exact
    A2R11-flagged defeat of the whole safety property: a floor can push
    the estimated peak PAST the working-set ceiling it was computed
    from). The final `min(configured, derived)` -- and the same
    zero-or-negative refusal applied to IT too -- is Section 16's
    'effective = min(configured, host-safe)': the operator-configured
    ceiling may only ever constrain this further, never expand past
    what the host can safely support."""
    configured = _resolve_configured_payload_budget()
    ceiling = _working_set_ceiling_bytes()
    available_for_new_build = ceiling - _RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES - prior_backup_bytes
    if available_for_new_build <= 0:
        raise AgentBackupError(
            f"insufficient memory budget for Agent Backup: this host's working-set ceiling "
            f"is {ceiling} byte(s) ({_RESOURCE_BUDGET_HOST_MEMORY_FRACTION:.0%} of "
            f"{_effective_available_memory_bytes()} byte(s) effective available memory), which "
            f"does not even cover this build's fixed overhead reserve "
            f"({_RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES} byte(s)) plus the existing destination "
            f"archive's own retained bytes ({prior_backup_bytes} byte(s)) -- refusing to attempt "
            f"a build this host cannot safely support (any existing destination archive is left "
            f"untouched, since this fails before any collection or publication step is reached)"
        )
    derived = available_for_new_build // _RESOURCE_BUDGET_SAFETY_FACTOR
    payload_limit = min(configured, derived)
    if payload_limit <= 0:
        raise AgentBackupError(
            f"insufficient memory budget for Agent Backup: the host-safe new-build payload "
            f"allowance derived from this host's working-set ceiling ({ceiling} byte(s)) is "
            f"{derived} byte(s) after reserving {_RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES} byte(s) "
            f"fixed overhead and {prior_backup_bytes} byte(s) for the existing destination "
            f"archive -- refusing to attempt a build this host cannot safely support (any "
            f"existing destination archive is left untouched, since this fails before any "
            f"collection or publication step is reached)"
        )
    return payload_limit


def _determine_verification_payload_budget(resident_bytes_hint: int = 0, member_count: int = 0) -> int:
    """AGENT-BACKUP-RESTORE-A2F11 (Section 11-13, repairing the A2R11 P0
    resource-policy blocker's second half): the host-safe decompression
    PHASE LIMIT for ANY verification that may call zf.testzip()/
    zf.read() -- builder self-verification, existing/prior-destination
    verification, standalone verify_agent_backup()/build_backup_receipt()
    calls, recovery-artifact verification, and any future Restore-
    oriented strict verification path all derive their decompression
    ceiling from this SAME host-safe working-set contract (Section 11's
    explicit 'no configured ceiling alone may authorize decompression
    the working-set contract would refuse' -- closing the exact gap
    where A2F10 deliberately used _resolve_configured_payload_budget()
    directly here instead).

    `resident_bytes_hint` is whatever bytes THIS call's own caller
    already knows are resident for reasons OTHER than the archive being
    verified itself (Section 3/10) -- e.g. a build's own already-
    collected new-member payload plus its own just-built candidate
    archive, both still resident while that SAME build now verifies its
    existing prior destination. A standalone caller with no such extra
    context passes 0, which is correct: THIS function's own caller chain
    (_verify_agent_backup_inner) separately, always adds the archive's
    OWN already-resident compressed bytes on top of this hint, since
    that part is common to every verification regardless of context.

    `member_count` (from trusted, un-decompressed central-directory
    metadata, never a hostile manifest's own self-reported count)
    charges a small, bounded per-member structural allowance (Section
    18) so a very large member COUNT is never treated as free merely
    because aggregate declared payload is small.

    Verification's own amplification factor
    (_VERIFICATION_AMPLIFICATION_FACTOR) is measured separately from --
    and is deliberately far smaller than -- the build side's
    (_RESOURCE_BUDGET_SAFETY_FACTOR): decompression's peak cost is
    dominated by the output buffer itself (Section 20's own empirical
    ~0.94x data point), not by the multiple simultaneously-resident
    copies a full build pipeline produces. Never raises -- an
    insufficient/negative allowance returns 0, letting the ordinary
    per-member/aggregate size checks in _verify_agent_backup_inner
    reject cleanly through the existing 'valid: False' contract rather
    than through a new exception path (Section 19: verification must
    never raise)."""
    configured = _resolve_configured_payload_budget()
    ceiling = _working_set_ceiling_bytes()
    structural = member_count * _RESOURCE_BUDGET_PER_MEMBER_STRUCTURAL_OVERHEAD_BYTES
    available = ceiling - _RESOURCE_BUDGET_FIXED_OVERHEAD_BYTES - resident_bytes_hint - structural
    if available <= 0:
        return 0
    derived = available // _VERIFICATION_AMPLIFICATION_FACTOR
    return max(0, min(configured, derived))


# Section 8-10 -- v1's maximum PHYSICAL ZIP entry count, strictly below
# the 0xFFFF (_ZIP64_SENTINEL_16) boundary at which a genuine entry
# count becomes physically indistinguishable from the ZIP64
# "read the real count from the ZIP64 EOCD locator instead" sentinel.
# AGENT-BACKUP-RESTORE-A2R10 measured the resulting ambiguity directly:
# with allowZip64=False, a real 65,534-entry archive builds and strictly
# verifies; a real 65,535-entry archive ALSO builds (zipfile's own
# ZIP_FILECOUNT_LIMIT check is `> 65535`, not `>= 65535`) but is legitimately
# indistinguishable from a ZIP64 sentinel and must be rejected either way;
# 65,536 entries raises zipfile.LargeZipFile outright. v1 eliminates the
# ambiguity entirely by never letting the builder ATTEMPT the ambiguous
# case: every physical member this module could ever emit, PLUS the one
# always-generated manifest.json, must total no more than this ceiling.
_MAX_PHYSICAL_ZIP_ENTRIES = 65534  # one less than the 0xFFFF ZIP64 sentinel value
# Deliberately NOT also exposing a separate "_MAX_COLLECTED_MEMBERS =
# _MAX_PHYSICAL_ZIP_ENTRIES - 1" constant (Section 9's own explicit
# warning: "derive this from actual generated-member count, not a
# brittle magic subtraction") -- _build_agent_backup_core instead
# computes `len(members) + 1` (the +1 being manifest.json, the one
# always-generated file today) at preflight time and compares that
# dynamic total directly against this one ceiling, so a future second
# always-generated file would not silently invalidate a separately
# hardcoded collected-member limit.

# Lumina's own v1 contract for known canonical members -- the verifier
# enforces these regardless of what an archive's own manifest claims
# (Section 7: "the verifier knows Lumina's own v1 contract", not just
# archive shape). AGENT-BACKUP-RESTORE-A2R3 found the OLD policy only
# fixed required/state_class/content_kind, leaving archive_path/portable/
# restore_policy/rebind_policy open to archive-authored redefinition
# (e.g. lumina_db portable=EXCLUDED, restore_policy=delete_without_
# restore) -- every field Lumina's v1 contract actually fixes for these
# members is enforced here now, sourced from
# AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md Section C's narrative
# table (the authoritative decision record), not blindly copied from
# that document's own illustrative JSON example, which disagrees with
# its own Section C table in one spot (lumina_db's portable value).
_CANONICAL_MEMBER_POLICY = {
    "state.databases.lumina_db": {
        "archive_path": "state/databases/lumina.db",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database",
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "overwrite_atomic_then_rewrite_skills_path_column",
        "rebind_policy": "rewrite_skills_path_column_to_dest_base_dir",
    },
    "state.telemetry.flight_recorder_db": {
        "archive_path": "state/telemetry/flight_recorder.db",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database",
        "portable": "PORTABLE",
        "restore_policy": "overwrite_atomic",
    },
    "state.preferences.prefs_json": {
        "archive_path": "state/preferences/prefs.json",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json",
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "overwrite_then_rewrite_last_persona_path",
        "rebind_policy": "rewrite_last_persona_absolute_path_to_dest_personas_dir",
    },
    "state.audit.tool_audit_log": {
        "archive_path": "state/audit/tool_audit.log",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "jsonl_log",
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": (
            "restore_verbatim_historical_only_never_auto_grants_"
            "executable_approval_on_destination"
        ),
        "rebind_policy": "quarantine_until_explicit_reapproval",
    },
    # AGENT-BACKUP-RESTORE-A2R4 (Section 8/11): pending_actions.json and
    # pending_actions_audit.log were previously enforced NOWHERE -- an
    # archive could freely redefine state_class/portable/restore_policy
    # or attach arbitrary fields (approved, autoexecute,
    # destination_authority, ...) to either with zero pushback, since
    # neither had a fixed-logical-id policy entry nor was subject to the
    # tight authority-bearing field allowlist. Both are FIXED (non-
    # parameterized) logical_ids, unlike per-project binding/chats below,
    # so they belong directly in this dict. Content survives an untrusted
    # archive; execution authority must not be created by archive-
    # authored metadata (A1's Forward Notice) -- the tight field
    # allowlist applied via _authority_bearing_allowed_fields_for is what
    # actually forecloses smuggling an authority field through, since
    # 'overwrite_atomic' itself carries none.
    "state.audit.pending_actions.json": {
        "archive_path": "state/audit/pending_actions.json",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json",
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    },
    "state.audit.pending_actions_audit.log": {
        "archive_path": "state/audit/pending_actions_audit.log",
        "required": True, "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "jsonl_log",
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    },
    # Fixed (non-parameterized) logical_ids from _collect_project_members/
    # _collect_metadata_members that were previously unenforced entirely
    # (Section 8's "at minimum define policy for... Project project.md
    # ... identity_trailers metadata").
    "overlay.projects.projectlist": {
        "archive_path": "overlay/projects/projectlist.md",
        "required": True, "state_class": "USER_OVERLAY",
        "content_kind": "markdown",
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    },
    "metadata.identity_trailers": {
        "archive_path": "metadata/identity_trailers.json",
        "required": False, "state_class": "USER_OVERLAY",
        "content_kind": "json",
        "portable": "PORTABLE",
        "restore_policy": "restore_verbatim_if_present_never_required_never_credentials",
    },
    "metadata.shipped_baseline_hashes": {
        "archive_path": "metadata/shipped_baseline_hashes.json",
        "required": False, "state_class": "USER_OVERLAY",
        "content_kind": "json",
        "portable": "PORTABLE",
        "restore_policy": "restore_verbatim_if_present_informational_only",
    },
}
# The subset of _CANONICAL_MEMBER_POLICY that a "full" backup_mode
# archive must always contain (Section 3) -- tool_audit_log is
# deliberately excluded: its source (tool_audit.log) only exists once a
# custom tool has ever been approved, a genuine valid absence, unlike
# the three unconditionally-required members below.
_REQUIRED_FLOOR_LOGICAL_IDS = (
    "state.databases.lumina_db",
    "state.telemetry.flight_recorder_db",
    "state.preferences.prefs_json",
)
_CANONICAL_LEDGER_LOGICAL_ID = "state.databases.ledger_db"
_CANONICAL_CREDENTIALS_EXCLUSION = {"logical_id": "credentials", "reason": "excluded_by_design"}

# Section 6 -- dynamic (per-file) custom-tool members can't be keyed by a
# fixed logical_id the way the canonical members above are, but their
# fields are still fixed by Lumina's v1 contract, and (since they carry
# potential executable authority on Restore) their field SET is a tight
# allowlist rather than an open schema -- an unrecognized field
# (approved, autoapprove, destination_authority, ...) can never smuggle
# authority through by using a name this module's own checks don't
# already inspect. state.audit.tool_audit_log gets the same allowlist
# treatment (its fixed values are enforced via _CANONICAL_MEMBER_POLICY
# above; only the field-set allowlist is shared here).
_CUSTOM_TOOL_FIXED_POLICY = {
    "required": True, "state_class": "REQUIRED_AGENT_STATE",
    "content_kind": "python_source", "portable": "PORTABLE_WITH_REBIND",
    "restore_policy": "restore_verbatim_then_quarantine_pending_reapproval",
    "rebind_policy": "quarantine_until_explicit_reapproval",
}
_AUTHORITY_BEARING_ALLOWED_FIELDS = {
    "logical_id", "state_class", "content_kind", "archive_path",
    "sha256", "size", "required", "portable", "restore_policy", "rebind_policy",
}
# Section 10 -- Project chats.json must not be able to declare MACHINE
# BINDING (a rebind_policy field) any more than it can declare
# executable authority -- its real v1 shape (_collect_project_members)
# never produces one, unlike binding.json/custom-tools/tool_audit_log,
# which legitimately do.
_CHATS_JSON_ALLOWED_FIELDS = _AUTHORITY_BEARING_ALLOWED_FIELDS - {"rebind_policy"}

# Section 8/11 -- FIXED (non-parameterized) logical_ids that also get the
# tight authority-bearing field allowlist (state.custom_tools.* and
# overlay.projects.*.binding_json/.chats_json are matched dynamically by
# prefix/suffix instead, in _authority_bearing_allowed_fields_for).
_FIXED_AUTHORITY_BEARING_LOGICAL_IDS = {
    "state.audit.tool_audit_log",
    "state.audit.pending_actions.json",
    "state.audit.pending_actions_audit.log",
}

# Section 8/9/10 -- trusted v1 policy families for DYNAMIC (per-file,
# per-project) logical_id patterns that _CANONICAL_MEMBER_POLICY's exact-
# match dict cannot key by (the logical_id is parameterized -- e.g.
# overlay.personas.<filename>, overlay.projects.<name>.<doc>).
# AGENT-BACKUP-RESTORE-A2R4 found these entirely unenforced: a hostile
# manifest could freely redefine a Project binding's state_class/
# portable/restore_policy/rebind_policy, a Project chats-linkage's
# state_class (including EXCLUDED -- Section 24's matrix), or any
# persona/skill/avatar/voice/tool-profile/project-doc's own restore
# mechanics, all with zero pushback. Each entry is (prefix, suffix,
# fixed_fields, archive_path_deriver) -- a member matches a family when
# its logical_id both starts with prefix and ends with suffix (suffix ""
# matches any logical_id sharing the prefix, used for the per-file
# overlay categories where every file in the directory shares one
# policy). content_kind is deliberately NOT fixed for avatars/voices --
# their real content_kind legitimately varies by file extension
# (json/markdown/binary, see _collect_overlay_dir), unlike every other
# family here where it never varies.
#
# archive_path_deriver(logical_id) -> the ONE archive_path a real
# collector would ever produce for that logical_id, or None if
# logical_id is malformed for this family (e.g. an empty extracted
# name/filename) -- AGENT-BACKUP-RESTORE-A2R5 (Section 16-19, BLOCKER):
# a member whose logical_id matched no family at all previously passed
# through with ZERO policy enforcement (a hand-built
# state.audit.totally_unconstrained_thing member, sitting under the
# otherwise-legitimate state/audit/ namespace, both verified valid=True
# AND inflated the receipt's rebind_required_count). _is_recognized_
# member (below) requires logical_id AND archive_path to correspond
# EXACTLY via this deriver, not merely both look independently
# plausible (Section 17: 'do not validate logical ID and archive path
# independently') -- closing near-match games too (Section 18):
# `binding.json.bak`/case variants/extra suffixes derive a DIFFERENT
# expected archive_path (or None), so they no longer belong to the
# family at all and fall through to the closed-world rejection.
def _is_safe_leaf_name(name: str) -> bool:
    """Section 21-24 (AGENT-BACKUP-RESTORE-A2R6, BLOCKER, A2F6): True iff
    `name` is a single, real, portable path SEGMENT -- never a path
    itself. Every flat overlay family (personas/skills/tool_profiles/
    avatars/voices) is collected via a flat os.listdir() of one directory
    (_collect_overlay_dir), never a walk, and every Project sub-file
    family's own project-name component comes from one directory-name
    listing (_project_dir_names) -- neither can EVER physically produce a
    '/'-separated value. A2R6 proved the OLD derivers only checked a
    suffix (fname.endswith('.json')) or bare truthiness, so a hand-built
    manifest could set logical_id='overlay.personas.nested/evil.json' (or
    'overlay.projects.parent/child.binding_json') and the deriver would
    faithfully reconstruct 'overlay/personas/nested/evil.json' -- an
    archive_path that MATCHES what the hostile manifest itself declared,
    so _is_recognized_member returned True for a physically impossible
    shape the real collector could never emit. This function is the
    single choke point closing that: reject '/', a backslash, and NUL
    outright (no normalized-equivalent path separator survives on any of Lumina's
    supported platforms), and reject exactly '.'/'..' (a real filename or
    project name can never legitimately be either). Deliberately permits
    everything else -- spaces, multiple dots, Unicode, mixed case, legal
    punctuation (Section 23/26: dots are not separators inside a
    user-controlled name; only these fixed prefix/suffix boundaries are).
    NOT applied to state.custom_tools.* -- _collect_custom_tool_members
    uses _safe_walk (a real recursive tree, e.g. _pending/staged_tool.py),
    so a '/'-separated custom-tool relpath is a genuine, legitimate
    collector output, not an impossible shape; general path-traversal
    segments ('..', an empty segment) inside it are already rejected
    elsewhere, uniformly for every family, by _check_path_and_identity_
    rules against the final archive_path itself."""
    if not name or "/" in name or "\\" in name or "\x00" in name:
        return False
    if name in (".", ".."):
        return False
    return True


def _deriver_custom_tools(logical_id):
    rel = logical_id[len("state.custom_tools."):]
    return f"state/custom_tools/{rel}" if rel and rel.endswith(".py") else None


def _deriver_personas(logical_id):
    fname = logical_id[len("overlay.personas."):]
    return f"overlay/personas/{fname}" if _is_safe_leaf_name(fname) and fname.endswith(".json") else None


def _deriver_skills(logical_id):
    fname = logical_id[len("overlay.skills."):]
    return f"overlay/skills/{fname}" if _is_safe_leaf_name(fname) and fname.endswith(".md") else None


def _deriver_tool_profiles(logical_id):
    fname = logical_id[len("overlay.tool_profiles."):]
    return f"overlay/tool_profiles/{fname}" if _is_safe_leaf_name(fname) and fname.endswith(".json") else None


def _deriver_avatars(logical_id):
    fname = logical_id[len("overlay.avatars."):]
    return f"overlay/avatars/{fname}" if _is_safe_leaf_name(fname) else None


def _deriver_voices(logical_id):
    fname = logical_id[len("overlay.voices."):]
    return f"overlay/voices/{fname}" if _is_safe_leaf_name(fname) else None


def _deriver_project_binding(logical_id):
    name = logical_id[len("overlay.projects."):-len(".binding_json")]
    return f"overlay/projects/{name}/binding.json" if _is_safe_leaf_name(name) else None


def _deriver_project_chats(logical_id):
    name = logical_id[len("overlay.projects."):-len(".chats_json")]
    return f"overlay/projects/{name}/chats.json" if _is_safe_leaf_name(name) else None


def _deriver_project_md(logical_id):
    name = logical_id[len("overlay.projects."):-len(".project_md")]
    return f"overlay/projects/{name}/project.md" if _is_safe_leaf_name(name) else None


def _deriver_codebase_md(logical_id):
    name = logical_id[len("overlay.projects."):-len(".codebase_md")]
    return f"overlay/projects/{name}/codebase.md" if _is_safe_leaf_name(name) else None


_DYNAMIC_MEMBER_POLICY_FAMILIES = (
    ("overlay.projects.", ".binding_json", {
        "state_class": "REBIND_REQUIRED", "content_kind": "json", "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "restore_then_require_owner_confirmation_of_root_path",
        "rebind_policy": "validate_root_exists_or_prompt_owner_to_repick",
    }, _deriver_project_binding),
    ("overlay.projects.", ".chats_json", {
        "state_class": "REQUIRED_AGENT_STATE", "content_kind": "json", "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic_with_lumina_db",
    }, _deriver_project_chats),
    ("overlay.projects.", ".project_md", {
        "state_class": "USER_OVERLAY", "content_kind": "markdown", "required": True,
        "portable": "PORTABLE", "restore_policy": "overwrite_atomic",
    }, _deriver_project_md),
    ("overlay.projects.", ".codebase_md", {
        "state_class": "REBUILDABLE_GENERATED", "content_kind": "markdown", "required": False,
        "portable": "REBUILD", "restore_policy": "restore_then_mark_stale_recommend_regenerate",
    }, _deriver_codebase_md),
    ("overlay.personas.", "", {
        "state_class": "USER_OVERLAY", "content_kind": "json", "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    }, _deriver_personas),
    ("overlay.skills.", "", {
        "state_class": "USER_OVERLAY", "content_kind": "markdown", "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    }, _deriver_skills),
    ("overlay.tool_profiles.", "", {
        "state_class": "USER_OVERLAY", "content_kind": "json", "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    }, _deriver_tool_profiles),
    ("overlay.avatars.", "", {
        "state_class": "USER_OVERLAY", "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    }, _deriver_avatars),
    ("overlay.voices.", "", {
        "state_class": "USER_OVERLAY", "required": True,
        "portable": "PORTABLE", "restore_policy": "baseline_aware_merge",
    }, _deriver_voices),
    # state.custom_tools.* carries no fixed_fields entry here -- its
    # fixed VALUES are enforced separately by _validate_authority_
    # bearing_member_schema (_CUSTOM_TOOL_FIXED_POLICY), unchanged since
    # A2F4. It still needs an archive_path deriver so the closed-world
    # check (_is_recognized_member) recognizes it as a real family.
    ("state.custom_tools.", "", {}, _deriver_custom_tools),
)


def _authority_bearing_allowed_fields_for(logical_id: str):
    """Returns the tight field allowlist for `logical_id` if it belongs
    to one of v1's authority-sensitive member classes (Section 13:
    'known v1 fields only' -- custom tools, tool audit, pending actions,
    and Project bindings; Section 10 additionally requires Project
    chats-linkage to reject a machine-binding-style field specifically),
    else None (an open/ordinary member, no field-set restriction beyond
    the manifest's normal structural schema)."""
    if logical_id.startswith("state.custom_tools."):
        return _AUTHORITY_BEARING_ALLOWED_FIELDS
    if logical_id in _FIXED_AUTHORITY_BEARING_LOGICAL_IDS:
        return _AUTHORITY_BEARING_ALLOWED_FIELDS
    if logical_id.startswith("overlay.projects.") and logical_id.endswith(".binding_json"):
        return _AUTHORITY_BEARING_ALLOWED_FIELDS
    if logical_id.startswith("overlay.projects.") and logical_id.endswith(".chats_json"):
        return _CHATS_JSON_ALLOWED_FIELDS
    return None

# Section 4 -- role-specific schema fingerprints proving archived bytes
# are genuinely Lumina's OWN primary database / Flight Recorder database,
# not merely any healthy SQLite file with a matching hash (AGENT-BACKUP-
# RESTORE-A2R3: an unrelated SQLite db with a valid quick_check, placed
# at state/databases/lumina.db with a correct hash/size, still verified
# valid=True). Table/column sets below are the tables core/agent.py's
# Agent.__init__ initializes UNCONDITIONALLY on every startup regardless
# of channel/owner (init_memory_db/init_chat_db/init_skills_db at
# core/agent.py:1437-1484) -- source-backed invariants, not fabricated.
# Deliberately NOT the full table list (palace/knowledge/coding_checkpoint
# tables are created lazily on first tool use, so requiring them would
# reject a genuinely valid, merely lightly-used, real lumina.db).
_LUMINA_DB_REQUIRED_TABLES = {
    "chats": {"id", "name", "created_at", "updated_at"},
    "chat_messages": {"id", "chat_id", "role", "content", "metadata", "created_at"},
    "memories": {"id", "label", "content", "created_at"},
    "skills": {"id", "name", "description", "path", "created_at", "updated_at"},
}
_FLIGHT_RECORDER_DB_REQUIRED_TABLES = {
    "events": {
        "seq", "ts", "runtime_id", "chat_id", "turn_id", "task_id",
        "process_id", "worktree_id", "activity_id", "epoch", "event_type",
        "severity", "provenance", "backend", "model", "fields_json", "expires_at",
    },
}

# Section 11 -- Windows portable-namespace ambiguities. A trailing dot or
# space is stripped/ignored by Windows filesystem APIs, so two distinct
# archive_paths that differ only in a trailing "." or " " would collide
# (or silently lose the suffix) on restore to a Windows destination --
# rejected outright rather than silently normalized. Reserved device
# names collide with an OS-level special file regardless of extension
# (CON.json still opens the console device on Windows); the archive
# namespace targets cross-platform round-tripping (members are tagged
# PORTABLE / PORTABLE_WITH_REBIND throughout this module, not Linux-only),
# so these are rejected too rather than silently accepted and left to
# fail unpredictably at restore time on a Windows destination.
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

# Only these top-level archive namespaces may ever physically exist in a
# v1 archive (Section 8) -- an ALLOWLIST, not a per-name blacklist, so a
# renamed/relocated forbidden payload (ledger stashed under an unexpected
# path, a worktree, a credentials alias) is caught by not being on this
# list, rather than by matching one specific expected bad filename.
_ALLOWED_ARCHIVE_PREFIXES = (
    "state/databases/", "state/telemetry/", "state/preferences/",
    "state/custom_tools/", "state/audit/",
    "overlay/personas/", "overlay/avatars/", "overlay/voices/",
    "overlay/skills/", "overlay/tool_profiles/", "overlay/projects/",
    "metadata/",
)
_FORBIDDEN_NAME_SEGMENTS = {"worktrees", "worktree", "scratch_tmp", "sandbox_tmp"}
_FORBIDDEN_BASENAMES = {"credentials.json", "credentials"}

_ROOT_FIELD_TYPES = {
    "format": str, "lumina_version": str, "created_at": str,
    "source_platform": dict, "source_data_root": dict, "backup_mode": str,
    "hash_algorithm": str, "compatibility": dict,
    "members": list, "regenerate": list, "exclusions": list, "warnings": list,
}


class AgentBackupError(Exception):
    """Raised when: a required member is missing, a source fails its
    trust-boundary check in a way that makes the whole backup untrustworthy,
    a SQLite snapshot cannot be produced consistently, directory enumeration
    fails, the destination is unsafe, or the finished archive fails its own
    post-write verification. Never raised for an ordinary, valid absence
    (a fresh install with no personas yet, no custom tools)."""


class AgentBackupDurabilityWarning(UserWarning):
    """AGENT-BACKUP-RESTORE-A2F11 (P1, Section 32-37): raised by
    build_agent_backup (never build_agent_backup_with_receipt, which has
    its own dedicated receipt["publication"]["directory_fsynced"] field
    for exactly this fact) when a publish's own parent-directory fsync
    did not succeed, even though the published content itself was
    independently re-observed and hash-confirmed. This is deliberately
    NOT folded into the returned manifest's own `warnings` list --
    Section 33's plain-API contract requires build_agent_backup to
    return the EXACT manifest embedded in the immutable archive it just
    published, and a fact only known AFTER publication (durability of
    the publish itself) cannot have been part of what was written into
    manifest.json before publication happened. A real Python warning
    (via the standard `warnings` module, filterable/catchable with
    ordinary `pytest.warns`/`warnings.catch_warnings` machinery) is the
    side channel this pass uses instead -- see build_agent_backup's own
    docstring for the full contract."""


# ---------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------
# Strict JSON decoder (AGENT-BACKUP-RESTORE-A2R9, Section 17-20, BLOCKER)
# -- one decoder for every authority-bearing JSON document this module
# parses (manifest.json, shipped_baseline_hashes.json, prefs.json).
# Unrestricted json.loads(...) silently applies last-key-wins semantics
# to a duplicate object key at ANY nesting depth, and happily parses the
# non-standard NaN/Infinity/-Infinity numeric tokens Python's json module
# tolerates as a non-interoperable extension -- neither is acceptable for
# a format whose fields carry restore/rebind/executable authority.
# ---------------------------------------------------------------------

class _DuplicateJSONKeyError(ValueError):
    """Raised by _strict_object_pairs_hook when a JSON object -- the root
    manifest object, a member/exclusion/regenerate record, or any nested
    policy/evidence object, at ANY depth -- repeats a key. A subclass of
    ValueError (like json.JSONDecodeError itself) so every existing call
    site's exception handling already covers it without modification."""


class _NonFiniteJSONConstantError(ValueError):
    """Raised by _reject_non_finite_json_constant on NaN/Infinity/
    -Infinity. Also a ValueError subclass for the same reason."""


def _strict_object_pairs_hook(pairs):
    """AGENT-BACKUP-RESTORE-A2R9 (Section 18): rejects a repeated key
    within one JSON object rather than silently keeping only the last
    occurrence (dict()'s own natural behavior on a duplicate-key pairs
    list). json.loads invokes this hook once per JSON object it decodes,
    bottom-up -- so this applies uniformly to the root manifest object
    AND every nested object inside it, with no separate wiring needed per
    nesting level."""
    seen = set()
    result = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateJSONKeyError(
                f"duplicate JSON object key {key!r} -- Agent Backup v1's strict "
                f"manifest JSON decoder rejects a repeated key at any nesting "
                f"depth rather than silently applying last-key-wins semantics"
            )
        seen.add(key)
        result[key] = value
    return result


def _reject_non_finite_json_constant(token: str):
    """AGENT-BACKUP-RESTORE-A2R9 (Section 19): json.loads's parse_constant
    hook fires for the bare tokens NaN/Infinity/-Infinity -- none of which
    is valid, interoperable JSON. Raising here (rather than returning
    float('nan')/float('inf') as the stdlib default behavior does) makes
    any one of them a hard parse failure."""
    raise _NonFiniteJSONConstantError(
        f"non-standard JSON numeric constant {token!r} is not permitted in "
        f"Agent Backup v1 JSON -- NaN/Infinity/-Infinity are not valid, "
        f"interoperable JSON values"
    )


def _strict_json_loads(raw):
    """THE one JSON decoder for every Agent Backup v1 authority-bearing
    document this module parses. Accepts bytes/bytearray (decoded as
    UTF-8, matching every call site's own prior decode-then-loads shape)
    or str. Raises ValueError (json.JSONDecodeError for ordinary
    malformed JSON, or one of the two dedicated subclasses above) on any
    rejection -- every call site already treats a broad parse exception
    as 'untrusted/malformed input, fall back to invalid or unknown', so
    none needs its own except clause narrowed or widened to adopt this."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw, object_pairs_hook=_strict_object_pairs_hook,
                       parse_constant=_reject_non_finite_json_constant)


def _read_lumina_version(base_dir: str) -> str:
    """Static source read of main.py's LUMINA_VERSION constant --
    deliberately never `import main`, which is the application entry
    point and may carry startup side effects far heavier than reading one
    constant. Falls back to 'unknown' (never a fabricated value) if the
    file or the constant can't be found."""
    main_py = os.path.join(base_dir, "main.py")
    try:
        with open(main_py, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return "unknown"
    match = re.search(r'^LUMINA_VERSION\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    return match.group(1) if match else "unknown"


# ---------------------------------------------------------------------
# Filesystem trust boundary (Blocker 2/3) -- identity decisions are bound
# to the opened descriptor, never to a separate pre-open stat() call.
# ---------------------------------------------------------------------

def _resolve_credentials_identity(credentials_path: str | None):
    """(st_dev, st_ino) of the canonical credentials store, or None if it
    doesn't exist. os.stat() reads metadata only -- this function never
    opens the file's contents. Resolution mirrors core/secrets.py's own
    default exactly (LUMINA_SECRETS_PATH env override, else
    ~/.config/lumina/credentials.json) without importing core.secrets."""
    path = credentials_path or os.environ.get("LUMINA_SECRETS_PATH") or \
        os.path.expanduser("~/.config/lumina/credentials.json")
    try:
        st = os.stat(path)  # follows symlinks on purpose: identity of the real target
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


_LEDGER_IDENTITY_LABEL = "ledger.db (REBUILDABLE_GENERATED -- A1 requires it is never archived)"
_CREDENTIALS_IDENTITY_LABEL = "canonical credentials store"


def _resolve_ledger_identity(data_dir: str):
    """(st_dev, st_ino) of ledger.db, or None if it doesn't exist.
    AGENT-BACKUP-RESTORE-A2R5 (Section 9, MEDIUM finding, confirmed
    live): a persona file hardlinked to ledger.db's inode had its real
    content -- ledger.db's own rows -- archived verbatim under the
    persona's archive_path, since the only forbidden identity ever
    checked anywhere in this module was credentials_identity. A1's own
    'ledger.db is never staged, snapshotted, hashed, or archived' is an
    ABSOLUTE guarantee, not scoped to any one collector, so its identity
    now joins the same forbidden-identity set credentials already used
    (Section 9's 'source-vet A1... for any other absolute never-archive
    source' -- ledger.db is the only other one A1 defines with a single,
    concrete, hardlink-aliasable file inode; scheduled_tasks/worktrees/
    scratch_tmp are EXCLUDED but are in-memory or whole-directory
    concepts, not one identifiable file inode to alias this way).
    Resolution mirrors core/idempotency.py's own LEDGER_PATH
    (DATA_DIR/memory/ledger.db) without importing idempotency.py, the
    same pattern _resolve_credentials_identity uses for core/secrets.py."""
    path = os.path.join(data_dir, "memory", "ledger.db")
    try:
        st = os.stat(path)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _build_forbidden_identities(credentials_identity, ledger_identity) -> dict:
    """The trusted set of forbidden source identities (Section 9) shared
    by both the lenient plain-file boundary (_open_verified, which
    excludes-with-warning on a match -- unchanged policy, now checking a
    wider set) and the strict state-bearing capture boundary
    (_open_state_bearing, which fails the whole backup on a match --
    Section 10's new requirement for required, discovered state)."""
    return {
        _CREDENTIALS_IDENTITY_LABEL: credentials_identity,
        _LEDGER_IDENTITY_LABEL: ledger_identity,
    }


def _classify_root(top_dir: str):
    """Returns (real_path_or_None, exists: bool, reason: str).

    exists=False means top_dir genuinely does not exist (ENOENT) -- a
    normal, valid absence for an optional/not-yet-created directory
    (a fresh install with no custom tools, no personas overlay, etc.).

    exists=True with real=None means top_dir is THERE but was rejected:
    either lstat itself failed for a reason OTHER than "doesn't exist"
    (permission denied, ELOOP, an I/O error -- callers for
    required-capable categories must escalate this, never silently treat
    it as empty; this is the fix for AGENT-BACKUP-RESTORE-A2R2's
    "custom-tools root becomes unreadable -> treated as absent"), or it
    exists but is a symlink or not a directory at all (a deliberate
    shape/security rejection -- callers may still warn-and-skip for
    these, consistent with the rest of this module's "a hostile or
    accidental symlink can never deny a backup" policy)."""
    try:
        lst = os.lstat(top_dir)
    except FileNotFoundError:
        return None, False, "does not exist"
    except OSError as e:
        return None, True, f"lstat failed: {e}"
    if stat.S_ISLNK(lst.st_mode):
        return None, True, "is a symlink -- refusing to treat as a trusted root"
    if not stat.S_ISDIR(lst.st_mode):
        return None, True, "exists but is not a directory"
    return os.path.realpath(top_dir), True, ""


def _validate_root(top_dir: str) -> str | None:
    """Convenience wrapper over _classify_root for callers that always
    escalate on ANY non-valid root regardless of reason (data_dir/base_dir
    top-level checks in _collect_members already raise unconditionally
    either way, so the exists/reason distinction doesn't change their
    behavior)."""
    real, _exists, _reason = _classify_root(top_dir)
    return real


def _require_readable_root(top_dir: str, label: str, warnings: list) -> str | None:
    """For roots whose contents are a REQUIRED-capable category (personas,
    skills, tool_profiles, avatars, voices, custom_tools, projects):
    genuine absence is fine (returns None, caller collects nothing);
    genuine unreadability escalates (AgentBackupError); a deliberate
    symlink/wrong-type rejection warns and returns None rather than
    escalating, preserving this module's "a hostile symlink can never
    deny future backups" policy."""
    real, exists, reason = _classify_root(top_dir)
    if real is not None:
        return real
    if not exists:
        return None
    if reason.startswith("lstat failed"):
        raise AgentBackupError(f"{label} root exists but could not be validated ({reason}): {top_dir}")
    warnings.append(f"excluded from backup ({reason}): {top_dir}")
    return None


def _open_verified(path: str, canonical_root_real: str, forbidden_identities: dict):
    """Opens `path` for reading with the credential/filesystem trust
    boundary decision bound to the ACTUALLY-OPENED descriptor, not a
    separate pre-open stat() call -- closes the hardlink-swap race where
    a path is repointed at a forbidden source's inode between a
    pre-open check and the read (AGENT-BACKUP-RESTORE-A2R2's "boundary
    check passes -> file swapped to hardlink of credentials -> open
    occurs -> credential bytes archived" reproduction; AGENT-BACKUP-
    RESTORE-A2R5 reproduced the identical class of gap via a ledger.db
    hardlink, which is why `forbidden_identities` (Section 9) is a set,
    not a single credentials_identity value, as of A2F5).

    This is the LENIENT trust boundary: a match excludes just the one
    file with a warning (never escalates the whole backup) -- correct
    for members that are not REQUIRED, discovered, state-bearing
    positions (Section 8: 'irrelevant unrecognized filesystem junk ->
    ignore/warn'; also used for optional metadata like identity_
    trailers.json). For a REQUIRED state-bearing member, callers must
    use _open_state_bearing/_stage_physical_state_file instead, which
    escalates a forbidden-identity match to AgentBackupError (Section
    10) rather than excluding it here.

    Returns (fd_or_None, reason, existence_failure). On success the
    caller owns the returned fd (regular-file, already fstat-verified)
    and must close it -- or hand it to os.fdopen(), which closes it for
    you.

    existence_failure=True means the lstat/open/fstat itself failed --
    the file is missing or became inaccessible, an availability problem,
    NOT a deliberate policy decision. existence_failure=False means the
    file demonstrably exists but is refused on purpose (symlink, wrong
    type, directory-symlink escape, credentials alias). Callers must NOT
    treat these the same: a REQUIRED source disappearing mid-staging
    escalates to a hard failure, while a security rejection must always
    just exclude the one file with a warning, regardless of required --
    so a hostile or accidental symlink can never be used to permanently
    deny every future backup.

    The lstat-based pre-filter below (symlink, wrong type, directory-
    symlink escape) is a cheap, non-security-critical fast path for the
    common case; the credentials-identity decision is made ONLY from
    fstat(fd) of what was actually opened -- never from a path-based
    os.stat() call that could observe a different file than the one that
    gets read afterward."""
    try:
        lst = os.lstat(path)
    except OSError as e:
        return None, f"lstat failed: {e}", True
    if stat.S_ISLNK(lst.st_mode):
        return None, "symlink -- never dereferenced", False
    if not stat.S_ISREG(lst.st_mode):
        return None, f"not a regular file (mode={oct(lst.st_mode)})", False
    real = os.path.realpath(path)
    if os.path.commonpath([real, canonical_root_real]) != canonical_root_real:
        return None, "escapes its canonical source root (directory-symlink traversal)", False

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        return None, f"open failed: {e}", True

    try:
        fst = os.fstat(fd)
    except OSError as e:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, f"fstat failed: {e}", True

    if not stat.S_ISREG(fst.st_mode):
        os.close(fd)
        return None, "opened descriptor is not a regular file", False
    identity = (fst.st_dev, fst.st_ino)
    for label, forbidden_identity in forbidden_identities.items():
        if forbidden_identity is not None and identity == forbidden_identity:
            os.close(fd)
            return None, f"aliases the {label} (opened descriptor device+inode)", False

    return fd, "", False


def _read_fd_fully_keep_open(fd: int) -> bytes:
    """Reads fd to EOF without ever closing it -- used by
    _read_stable_bytes_or_none, which needs to fstat/reread the SAME
    descriptor a second time after the first read completes."""
    chunks = []
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


# AGENT-BACKUP-RESTORE-A2R6 (Section 7-10, BLOCKER): a stable, uniquely-
# linked, non-forbidden, safely-opened DESCRIPTOR (everything
# _open_state_bearing already proves) does not by itself prove the BYTES
# read through it are a single coherent version -- a concurrent writer
# holding the same inode open can still mutate it mid-read (truncate,
# append, overwrite-beginning/middle/end, or a byte-for-byte
# size-preserving rewrite -- A2R6 specifically defeated a size-only
# staleness check with the last one) and produce a torn, hybrid capture
# that may remain syntactically valid (e.g. still-parseable JSON stitched
# from two different real versions) and therefore pass every check
# downstream. size/mtime alone is insufficient by construction (a
# size-preserving rewrite changes neither), so every field a rewrite
# could plausibly leave untouched is compared, never just one.
_STABILITY_STAT_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns",
)


def _stability_tuple(st) -> tuple:
    return tuple(getattr(st, field) for field in _STABILITY_STAT_FIELDS)


def _read_stable_bytes_or_none(fd: int):
    """Consumes and ALWAYS closes fd (mirrors _read_fd_fully's ownership
    contract). Proves the bytes read through it are a single, coherent,
    repeatable version via three fstat calls bracketing two full reads of
    the same already-open, already-trust-boundary-verified descriptor:

        fstat A -> read bytes_1 -> fstat B -> seek(0) -> read bytes_2 -> fstat C

    Returns (bytes_1, True) only if the stability tuples (device, inode,
    mode, link count, size, mtime_ns, ctime_ns) of A, B, and C are all
    IDENTICAL and bytes_1 == bytes_2 -- proving both that no metadata
    changed underneath this read and that the exact same content is
    independently reproducible from the same descriptor, not merely one
    lucky read that happened to land between two writes. Returns
    (None, False) on any instability; the caller (_stage_required_state_
    file) owns the retry/fail-closed decision -- this function never
    retries itself and never returns a torn/partial capture under any
    circumstance.

    Deliberately never applied to SQLite sources -- those already have
    their own point-in-time-consistent snapshot mechanism via the online
    backup API (_snapshot_sqlite), which this primitive is not a
    replacement for and must not be layered onto (Section 10)."""
    try:
        st_a = os.fstat(fd)
        data1 = _read_fd_fully_keep_open(fd)
        st_b = os.fstat(fd)
        if _stability_tuple(st_a) != _stability_tuple(st_b):
            return None, False
        os.lseek(fd, 0, os.SEEK_SET)
        data2 = _read_fd_fully_keep_open(fd)
        st_c = os.fstat(fd)
        if _stability_tuple(st_b) != _stability_tuple(st_c):
            return None, False
        if data1 != data2:
            return None, False
        return data1, True
    finally:
        os.close(fd)


def _open_state_bearing(path: str, canonical_root_real: str, forbidden_identities: dict,
                         expected_identity=None):
    """THE security boundary for a REQUIRED, discovered, state-bearing
    member (Section 5/6, A2F5) -- repairing AGENT-BACKUP-RESTORE-A2R5's
    discovery-to-stage TOCTOU finding: A2F4 checked special-file/symlink
    shape via a SEPARATE, EARLIER lstat() (_reject_if_special_file_or_
    symlink) and only staged the bytes later, through the ordinary
    lenient _open_verified/_stage_plain_file path -- A2R5 raced a real
    persona between those two moments (swap to a symlink after the
    discovery-time check had already passed) and the LATER lenient path
    quietly excluded it with a warning instead of failing the backup,
    defeating Section 4/5's entire guarantee. There is no discovery-time
    check here at all to race against: the security decision is made
    from the SAME open+fstat that captures the bytes, in one call, with
    no gap.

    Never dereferences a symlink (O_NOFOLLOW): a symlink at the final
    path component makes the open() itself fail with ELOOP, atomically,
    as part of the SAME syscall that would otherwise have opened it --
    there is no window between 'detect symlink' and 'open the file'
    because they are not two separate operations. O_NONBLOCK is set
    alongside O_NOFOLLOW so that a FIFO or certain special files can
    never make this call HANG waiting for a writer/peer that will never
    come (a regular file's later reads are unaffected by O_NONBLOCK --
    it only changes behavior for FIFOs/sockets/some device files, per
    POSIX); a Unix domain socket path fails open() outright with ENXIO
    on Linux, also without blocking. Both ELOOP and ENXIO, along with a
    successfully-opened-but-non-regular descriptor (a directory, a
    device that didn't raise), are reported as kind='special' -- the
    Section 5 FAIL-BACKUP case for a required state-bearing member.

    Directory-escape detection resolves the descriptor's OWN canonical
    path via /proc/self/fd/<fd> when available (the same technique
    _connect_sqlite_with_bound_identity uses for connection identity --
    immune to a later rename of the pathname used to open it), falling
    back to a path-based os.path.realpath() only when /proc is
    unavailable (matching this module's pre-A2F5 behavior exactly in
    that fallback case, not a new weakness).

    AGENT-BACKUP-RESTORE-A2R6 (Section 1-3, BLOCKER, A2F6): A2F5's
    forbidden_identities set stopped a hardlink to credentials/ledger.db
    specifically, by resolving their identities ONCE, up front, and
    comparing every candidate against that snapshot. A2R6 proved this
    can never be complete: replace credentials.json (or ledger.db) with
    a new inode after that snapshot was taken, then hardlink a state
    position to the NEW inode -- it is not in the remembered forbidden
    set, so the alias sails through untouched, and the same rotation
    works against any future excluded source this module doesn't even
    know about yet. Chasing an ever-widening blacklist of rotating
    forbidden identities is not a fixable strategy. v1's actual answer
    (Section 3, this module's own house style -- see tools/file_edit.py's
    pre-existing 'refusing to edit a hard-linked file (nlink=N)' policy,
    the same category of guarantee applied here for the same reason) is
    categorical: a state-bearing file may never be a hardlink AT ALL,
    regardless of what its other name(s) point to right now or will ever
    point to. This closes the entire alias class in one motion, rather
    than reactively enumerating members of it.

    Returns (fd_or_None, reason, kind). kind is one of:
      'ok'          -- fd is a verified, safely-opened regular file, not
                       aliasing any forbidden_identities entry, uniquely
                       linked (st_nlink == 1), and (if expected_identity
                       was given) matching it. Caller owns fd, must
                       consume/close it.
      'absent'      -- genuinely does not exist right now (ENOENT) --
                       the ordinary, valid case for an optional slot the
                       caller has already decided is fine to be absent.
      'special'     -- exists but is not (or cannot safely be proven to
                       be) a regular file -- symlink, FIFO, socket,
                       device, directory. Never returns an fd.
      'hardlink'    -- opened successfully, is a genuine regular file,
                       and is not a forbidden-identity alias, but its
                       descriptor-bound st_nlink is > 1 (AGENT-BACKUP-
                       RESTORE-A2R6, Section 2-3, BLOCKER: see this
                       function's own docstring above for why a
                       forbidden-identity *blacklist* can never fully
                       enumerate every alias a hardlink might create --
                       v1's answer is a categorical 'a state-bearing file
                       may never be a hardlink at all', checked here
                       regardless of what its other name(s) point to).
                       Never returns an fd (already closed).
      'forbidden'   -- opened successfully but its descriptor-bound
                       identity matches a forbidden_identities entry, or
                       (when expected_identity is given) does not match
                       it. Never returns an fd (already closed). Checked
                       BEFORE the 'hardlink' outcome above, so a
                       credentials/ledger alias still reports the more
                       specific, more actionable reason.
      'unavailable' -- a different OSError (permission denied, escapes
                       canonical root, fstat failure) -- an availability
                       problem, not a deliberate special-file/identity
                       rejection, but still never returns an fd."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None, "does not exist", "absent"
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENXIO):
            return None, f"exists but cannot be safely opened as a regular file ({e})", "special"
        return None, f"open failed: {e}", "unavailable"

    try:
        fst = os.fstat(fd)
    except OSError as e:
        try:
            os.close(fd)
        except OSError:
            pass
        return None, f"fstat failed: {e}", "unavailable"

    if not stat.S_ISREG(fst.st_mode):
        os.close(fd)
        return None, f"opened descriptor is not a regular file (mode={oct(fst.st_mode)})", "special"

    if os.path.isdir(_PROC_FD_DIR):
        try:
            real = os.readlink(os.path.join(_PROC_FD_DIR, str(fd)))
        except OSError:
            real = os.path.realpath(path)
    else:
        real = os.path.realpath(path)
    if os.path.commonpath([real, canonical_root_real]) != canonical_root_real:
        os.close(fd)
        return None, "escapes its canonical source root (directory-symlink traversal)", "unavailable"

    identity = (fst.st_dev, fst.st_ino)
    for label, forbidden_identity in forbidden_identities.items():
        if forbidden_identity is not None and identity == forbidden_identity:
            os.close(fd)
            return None, f"aliases the {label} (opened descriptor device+inode)", "forbidden"

    if expected_identity is not None and identity != expected_identity:
        os.close(fd)
        return None, "opened descriptor does not match the verified expected identity", "forbidden"

    if fst.st_nlink != 1:
        os.close(fd)
        return None, (
            f"is a hardlink (st_nlink={fst.st_nlink}) -- v1 policy requires every "
            f"state-bearing file to be uniquely linked, regardless of what its "
            f"other name(s) point to (a forbidden-identity blacklist can never "
            f"fully enumerate every alias a hardlink might create; see AGENT-"
            f"BACKUP-RESTORE-A2R6)"
        ), "hardlink"

    return fd, "", "ok"


# Section 11 -- a small BOUNDED retry when a concurrent write is caught
# mid-read: one fresh attempt from a brand-new secure open, never
# unbounded spinning and never a silently-accepted torn read.
_MAX_STABLE_CAPTURE_ATTEMPTS = 2


def _open_and_capture_stable_bytes(path: str, canonical_root_real: str,
                                    forbidden_identities: dict, expected_identity=None):
    """THE universal physical-file capture loop (AGENT-BACKUP-RESTORE-
    A2R7, Section 2-4, BLOCKER, A2F7): every ZIP payload sourced from a
    mutable filesystem pathname -- REQUIRED state or merely OPTIONAL
    metadata, it makes no difference -- goes through this ONE function.
    A2R6 found _open_state_bearing/_read_stable_bytes_or_none only wired
    into required-state collectors; A2R7 found the OTHER three physical
    call sites (Project codebase.md, identity_trailers.json, shipped_
    baseline_hashes.json) still used the old, weak _open_verified + a
    single _read_fd_fully() -- no nlink check, no stability proof, no
    retry. 'Optional' has only ever meant ONE thing correctly in this
    module: a genuinely absent source is a valid, non-escalating outcome.
    It has never meant a PRESENT file gets a weaker security boundary --
    that was simply a real implementation gap this closes.

    Combines _open_state_bearing (regular file, st_nlink==1, forbidden-
    identity check, directory-escape check, optional expected_identity
    match) with _read_stable_bytes_or_none's bracketed double-read
    (fstat/read/fstat/seek/read/fstat, metadata AND content agreement),
    retried once from a brand-new secure open on a detected instability
    (Section 11's bounded retry -- never unbounded spinning, never a
    silently-accepted torn read).

    Returns (data_or_None, kind, reason, attempts_used). kind is:
      'ok'        -- data is the proven-stable bytes; attempts_used
                     tells the caller whether a retry was needed (>1).
      'absent'    -- source genuinely does not exist right now -- the
                     caller's own decision whether that's fine.
      'unstable'  -- every attempt's bytes failed the stability proof --
                     a persistent concurrent writer, never resolved.
      anything else -- an _open_state_bearing outcome verbatim ('special',
                     'forbidden', 'hardlink', 'unavailable') -- a
                     deliberate security rejection or availability
                     problem, never merely excludes-with-a-warning."""
    reason = ""
    for attempt in range(1, _MAX_STABLE_CAPTURE_ATTEMPTS + 1):
        fd, reason, kind = _open_state_bearing(path, canonical_root_real,
                                                forbidden_identities, expected_identity)
        if kind != "ok":
            return None, kind, reason, attempt
        try:
            data, stable = _read_stable_bytes_or_none(fd)
        except OSError as e:
            return None, "unavailable", f"could not be read ({e})", attempt
        if stable:
            return data, "ok", "", attempt
    return None, "unstable", (
        "descriptor metadata or content changed across a bracketed double-read "
        "-- a concurrent mutation was detected mid-capture, and persisted across "
        f"{_MAX_STABLE_CAPTURE_ATTEMPTS} attempts from brand-new secure opens"
    ), _MAX_STABLE_CAPTURE_ATTEMPTS


def _stage_physical_state_file(source_path: str, staging_root: str, archive_path: str,
                                canonical_root_real: str, forbidden_identities: dict,
                                label: str, warnings: list, expected_identity=None,
                                budget: "_AggregateBudget | None" = None):
    """Captures ANY physical, filesystem-sourced ZIP member's bytes via
    the universal capture boundary above (Section 2-4, A2R7/A2F7) -- the
    SAME function whether the position is REQUIRED_AGENT_STATE (a
    persona, prefs.json, tool_audit.log, a Project binding.json) or
    merely optional/metadata/rebuildable (codebase.md, identity_
    trailers.json, shipped_baseline_hashes.json). 'required' vs
    'optional' is ENTIRELY the calling collector's own business -- it is
    decided solely by how the caller reacts to a (None, None) return
    (raise, for a position that must exist; skip the member, for one
    that need not). This function itself never treats absence as an
    error and never treats presence-with-a-security-problem as anything
    softer than a whole-backup failure, for ANY caller -- there is no
    'lenient' variant left for a real physical file. manifest.json is the
    one genuinely different category this function is deliberately never
    used for: it is constructed entirely in memory and never staged from
    a pathname at all (Section 7).

    AGENT-BACKUP-RESTORE-A2R9 (Section 2-8, BLOCKER B1): prior to this
    pass, only ONE dual-use physical member (shipped_baseline_hashes.json,
    via the now-removed sibling _capture_and_stage_physical_state_file)
    returned its captured bytes at all -- every OTHER physical member
    returned only a staged PATHNAME, which _build_agent_backup_core then
    reopened by name (_sha256_file) for the manifest hash/size, and the
    zip-writing step reopened AGAIN (zipfile.write) for the archived
    payload -- three independent reads of the same mutable staging
    pathname, each a substitution opportunity for a same-UID, same-
    process attacker (A2R9 proved this live: swap the staged file's
    content between the hash read and the zip-write read, and the
    archive contains the substituted bytes with a manifest hash that
    still matches, because the hash was computed from a DIFFERENT read of
    the same mutable path). v1's answer: EVERY physical member now
    returns its captured bytes here, at the one and only moment its
    source is opened -- (data, staged_path). `data` is the member's
    authoritative content from this point forward, for hashing, for
    manifest construction, and for the ZIP payload (zf.writestr, never
    zf.write) alike; `staged_path` still exists (some collectors/tests
    still reference it, and it is occasionally useful for diagnostics)
    but is transport/cache only from here on -- NOTHING in this module
    ever reopens it by name again to determine payload bytes, a hash, a
    size, or manifest/ZIP content (Section 4/11).

    Returns (data, staged_path), or (None, None) if source_path genuinely
    does not exist right now. Any other outcome -- a symlink, a FIFO/
    socket/device/directory, a forbidden-identity alias (credentials,
    ledger.db), a hardlink (st_nlink > 1), an identity mismatch, a
    persistently unstable read, or a genuine I/O failure -- raises
    AgentBackupError: FAIL THE WHOLE BACKUP. A retry that recovers a
    transient instability is recorded truthfully in `warnings`, never
    silently absorbed."""
    data, kind, reason, attempts = _open_and_capture_stable_bytes(
        source_path, canonical_root_real, forbidden_identities, expected_identity)
    if kind == "absent":
        return None, None
    if kind != "ok":
        raise AgentBackupError(
            f"{label} exists but cannot be faithfully captured ({reason}): {source_path} "
            f"-- refusing to silently omit it (or exclude it with a mere warning) from a "
            f"backup that would otherwise claim completeness (SECURITY_REJECTED_STATE)"
        )
    if attempts > 1:
        warnings.append(
            f"{label} required a stable-capture retry before it could be read "
            f"as a single coherent version (a concurrent write was detected and "
            f"safely discarded on an earlier attempt, not silently accepted): "
            f"{source_path}"
        )
    # AGENT-BACKUP-RESTORE-A2F10 (HIGH H2, Section 22): charge the
    # aggregate resource budget the INSTANT bytes are captured -- before
    # they are even written to staging, and long before every member's
    # bytes could otherwise accumulate unbounded across a whole build.
    if budget is not None:
        budget.charge(len(data), label)
    staged_path = os.path.join(staging_root, archive_path)
    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
    with open(staged_path, "wb") as f:
        f.write(data)
    return data, staged_path


def _safe_listdir(dir_path: str) -> list[str]:
    """os.listdir(), but an enumeration failure on a directory that DOES
    exist is escalated loudly -- 'directory exists and traversal failed'
    must never be silently treated as 'empty' (A2R's Blocker 3 finding)."""
    try:
        return sorted(os.listdir(dir_path))
    except OSError as e:
        raise AgentBackupError(f"failed to enumerate directory {dir_path}: {e}")


def _safe_walk(top_dir: str):
    """os.walk(), but wrapped so onerror escalates instead of being
    silently swallowed (os.walk's default onerror=None discards
    exceptions from listdir on subdirectories it can't read) -- and this
    escalation fires for the TOP directory itself too, not just
    subdirectories (AGENT-BACKUP-RESTORE-A2R2's "custom-tools root
    becomes unreadable -> treated as absent")."""
    def _onerror(exc):
        raise AgentBackupError(f"failed to enumerate directory tree under {top_dir}: {exc}")
    return os.walk(top_dir, onerror=_onerror)


# ---------------------------------------------------------------------
# SQLite snapshot via the online backup API (Blocker 1), now with the
# same trust boundary as every other source (Blocker 2) and a timeout
# that actually bounds SQLite's own busy-handler (Section 13).
# ---------------------------------------------------------------------

class _SnapshotDeadline(Exception):
    pass


def _verify_sqlite_source_identity(source_path: str, canonical_root_real: str, forbidden_identities: dict):
    """Same trust-boundary decision as _open_verified, applied to a
    SQLite source -- but sqlite3's stdlib API requires a real filesystem
    PATH (not a bound file descriptor) so it can find a WAL-mode
    database's sibling -wal/-shm files at <path>-wal / <path>-shm.
    Redirecting it through e.g. /proc/self/fd/N would silently defeat WAL
    detection (SQLite would see no sibling file at that synthetic path
    and read a possibly-stale main file instead) -- so the identity
    decision here cannot be bound to the same descriptor SQLite itself
    reads through, the way _open_state_bearing/_stage_physical_state_file's
    can for plain files.

    Instead: open+fstat+close a throwaway verification descriptor
    (closing the symlink-swap race completely -- O_NOFOLLOW defeats a
    symlink substitution outright) and return its (dev, ino) identity so
    the caller can call this again immediately after sqlite3.connect()
    succeeds and compare identities, catching a hardlink-swap that
    happens in the narrow remaining gap. This is a documented, narrower
    mitigation than the plain-file case for exactly this reason -- not a
    claim of the same fd-binding guarantee. _snapshot_sqlite calls this
    twice (before connect, and immediately after) plus once more inside
    the file existence check, so the residual window is the time between
    two adjacent open()+fstat() pairs, not the whole staging pipeline.

    Returns (ok, reason, existence_failure, identity_or_None)."""
    fd, reason, existence_failure = _open_verified(source_path, canonical_root_real, forbidden_identities)
    if fd is None:
        return False, reason, existence_failure, None
    try:
        fst = os.fstat(fd)
    finally:
        os.close(fd)
    return True, "", False, (fst.st_dev, fst.st_ino)


_PROC_FD_DIR = "/proc/self/fd"

# AGENT-BACKUP-RESTORE-A2R4 (R4-B1): serializes every call to
# _connect_sqlite_with_bound_identity so this module's OWN before/after
# fd snapshots can never interleave with ANOTHER call this module makes
# (e.g. a future caller snapshotting lumina.db and flight_recorder.db
# concurrently from two threads). This is a narrowly-scoped mitigation
# for this module's own contribution to before/after ambiguity -- it
# cannot and does not claim to serialize every other fd-opening/closing
# code path elsewhere in a live, multi-threaded Lumina process; see the
# docstring below for the honest, documented residual limitation this
# implies.
_SQLITE_CONNECT_SERIALIZE_LOCK = threading.Lock()


def _proc_fd_identity_snapshot(context: str) -> dict:
    """Returns {fd_number: (st_dev, st_ino)} for every one of this
    process's currently-open file descriptors that can still be stat()'d
    at the moment of the call. A descriptor that closes between
    os.listdir() and os.stat() (e.g. the listing's own transient scan
    descriptor) is simply absent from the result -- not an error, and
    never conflated with 'this fd number does not exist.'

    Raises AgentBackupError (fail closed) if the directory itself cannot
    be enumerated at all (e.g. /proc/self/fd exists but a permission
    error prevents listing it) -- distinct from individual entries
    disappearing mid-scan, which is expected and harmless."""
    try:
        names = os.listdir(_PROC_FD_DIR)
    except OSError as e:
        raise AgentBackupError(
            f"cannot cryptographically bind the SQLite connection ({context}): "
            f"failed to enumerate this process's open file descriptors ({e}) "
            f"-- fail closed, connection discarded rather than trusted unverified"
        )
    snapshot = {}
    for name in names:
        try:
            fd_num = int(name)
        except ValueError:
            continue
        try:
            st = os.stat(os.path.join(_PROC_FD_DIR, name))
        except OSError:
            continue  # closed again already, or transient (e.g. the listing's own scan fd)
        snapshot[fd_num] = (st.st_dev, st.st_ino)
    return snapshot


def _newly_acquired_identities(before: dict, after: dict) -> set:
    """The set of (st_dev, st_ino) identities newly attributable to
    whatever happened between `before` and `after` -- keyed by
    (fd_number -> identity) DELTA, not by fd-number presence alone.
    AGENT-BACKUP-RESTORE-A2R4 (R4-B1): an earlier before/after attempt
    compared NAME (fd-number) sets, which is provably insufficient --
    if an unrelated fd number is closed and immediately reused (by
    completely unrelated code) for a different file between the two
    snapshots, comparing bare fd-number sets sees 'same name, nothing
    new' and silently misses the identity change entirely. Comparing
    (fd_number, identity) pairs closes this: a fd number is only
    excluded from the result if it existed in `before` bound to the
    EXACT SAME identity it has in `after` -- unchanged, not merely
    present."""
    return {identity for fd, identity in after.items() if before.get(fd) != identity}


def _connect_sqlite_with_bound_identity(open_fn, source_path: str,
                                         expected_identity, credentials_identity):
    """Calls open_fn() (a zero-arg callable performing sqlite3.connect())
    and proves, from file descriptor(s) NEWLY ACQUIRED BY THIS SPECIFIC
    CALL, that it is bound to expected_identity and never to
    credentials_identity -- closing two distinct attacks:

    AGENT-BACKUP-RESTORE-A2R3's connect-time hardlink-swap-and-restore:

        safe lumina.db exists
        -> sqlite3.connect(path) begins
        -> pathname swapped to a hardlink of the credentials store
        -> connection opens the credentials inode
        -> pathname restored to the safe lumina.db
        -> a PATHNAME-based post-connect re-stat() sees the safe path
           and is satisfied -- but the connection still holds the
           credentials inode open
        -> backup publishes credentials

    AGENT-BACKUP-RESTORE-A2R4's process-wide-membership hall pass (what
    A2F3's own defense against the above was still vulnerable to): A2F3
    proved expected_identity/credentials_identity by checking whether
    EITHER appeared ANYWHERE in a single static listing of every fd this
    PROCESS currently holds open -- not specifically among the fds THIS
    open_fn() call itself acquired. A2R4 exploited exactly that gap: park
    an unrelated, already-open connection resting on the expected inode
    BEFORE calling this function, then let open_fn()'s own connect
    actually open attacker-selected (but non-credentials) bytes at a
    DIFFERENT inode. The resting connection's fd supplies 'expected_
    identity is present somewhere in the process,' the attacker's own
    new fd never touches credentials_identity either, and the old
    process-wide-membership check waved the whole thing through even
    though THIS connection never touched the expected inode at all.

    The fix: take an identity-keyed snapshot of every open fd
    IMMEDIATELY BEFORE calling open_fn(), another IMMEDIATELY AFTER, and
    examine only the fds whose (fd_number -> identity) binding is NEW or
    CHANGED between the two (_newly_acquired_identities) -- never the
    full post-connect set. A resting, unrelated connection (on the
    expected inode, on the credentials inode, or on anything else) that
    was ALREADY open before this call started is therefore excluded from
    consideration entirely, regardless of what it happens to be resting
    on: only fds this open_fn() invocation itself caused to newly exist
    (or to change identity, catching a closed-and-reused fd NUMBER too --
    see _newly_acquired_identities) can satisfy either check below.
    Re-running the A2R3 hardlink-swap-and-restore reproduction through
    this same mechanism still catches it exactly as before: the
    connection's own newly-opened fd is bound to the credentials inode
    regardless of what the pathname is restored to afterward, and that
    fd was not already open beforehand, so it is unconditionally in the
    'newly acquired' set this function actually inspects.

    Neither snapshot ever stats a PATH. /proc/self/fd/N is a magic
    symlink; os.stat() on it (which follows the symlink) returns the
    metadata of the file description that fd is CURRENTLY bound to --
    fixed permanently the moment the underlying open() syscall succeeded,
    regardless of any later rename/relink of the pathname used to open
    it. There is therefore no further race once open_fn() has returned:
    whatever inode a live fd is bound to cannot change out from under it.

    Empirically confirmed (not assumed) that sqlite3.connect() -- even in
    read-only URI mode, even before any query -- opens the main database
    file's descriptor synchronously within the connect() call itself, so
    it is always present in the post-connect snapshot.

    Concurrency (Section 3's explicit ask): the before/after pair for a
    single call is taken under _SQLITE_CONNECT_SERIALIZE_LOCK, so two
    calls THIS MODULE makes (e.g. a future concurrent snapshot of
    lumina.db and flight_recorder.db) can never interleave their own
    snapshots with each other. This is a narrowly-scoped mitigation for
    this module's own contribution to before/after ambiguity, honestly
    documented as such -- it cannot serialize every fd-opening/closing
    code path elsewhere in a live, multi-threaded Lumina process (e.g.
    an ordinary chat query concurrently opening the very same lumina.db
    path in another thread). Any such coincidental, unrelated, same-
    process activity that happens to open a NEW fd bound to
    expected_identity during the exact connect() window would be
    indistinguishable, by this mechanism alone, from this call's own
    fd -- a real, accepted, and honestly-recorded residual limitation of
    a userspace-only, non-cryptographic, Linux /proc-based v1 identity
    proof (Section 3: 'a Linux-specific A2 v1 implementation is
    acceptable if documented honestly'). It does NOT reopen A2R4's own
    reported gap: that gap required an unrelated connection resting on
    the expected inode BEFORE this call began, which this fix already
    excludes unconditionally via the before-snapshot.

    Raises AgentBackupError (fail closed, connection discarded) on:
    /proc/self/fd unavailable on this platform (never silently falls
    back to a weaker path- or process-wide-membership-based check --
    'if the platform cannot prove connection identity: fail closed'),
    failure to enumerate open descriptors before or after connecting, no
    fd newly attributable to this call at all (cannot prove ANY
    attribution), a newly-acquired descriptor bound to
    credentials_identity (REJECTED -- possible swap attack), or no
    newly-acquired descriptor bound to expected_identity (cannot prove
    THIS connection is the verified one)."""
    if not os.path.isdir(_PROC_FD_DIR):
        raise AgentBackupError(
            f"cannot cryptographically bind the SQLite connection for "
            f"{source_path} to a verified filesystem identity on this "
            f"platform ({_PROC_FD_DIR} unavailable) -- refusing to snapshot "
            f"an unverified connection rather than silently weaken the "
            f"credentials guarantee (fail closed)"
        )

    with _SQLITE_CONNECT_SERIALIZE_LOCK:
        before = _proc_fd_identity_snapshot(f"pre-connect snapshot for {source_path}")
        conn = open_fn()
        try:
            after = _proc_fd_identity_snapshot(f"post-connect snapshot for {source_path}")
        except AgentBackupError:
            conn.close()
            raise

    new_identities = _newly_acquired_identities(before, after)

    if not new_identities:
        conn.close()
        raise AgentBackupError(
            f"could not attribute any newly-opened file descriptor to the "
            f"SQLite connection just opened for {source_path} -- refusing "
            f"to trust a connection whose own descriptors cannot be "
            f"distinguished from pre-existing process state (fail closed)"
        )

    if credentials_identity is not None and credentials_identity in new_identities:
        conn.close()
        raise AgentBackupError(
            f"REJECTED: the live SQLite connection just opened for "
            f"{source_path} newly acquired a file descriptor bound to the "
            f"same device+inode as the canonical credentials store -- this "
            f"is the connect-time hardlink-swap attack (the pathname may "
            f"already be restored to a safe value by the time any "
            f"path-based check would run, but the descriptor THIS "
            f"connection just acquired still refers to the credentials "
            f"inode). Refusing to snapshot or publish anything from this "
            f"connection."
        )

    if expected_identity is not None and expected_identity not in new_identities:
        conn.close()
        raise AgentBackupError(
            f"could not prove the live SQLite connection just opened for "
            f"{source_path} newly acquired a file descriptor bound to the "
            f"same verified inode that passed the pre-connect trust-"
            f"boundary check -- a pre-existing, unrelated connection "
            f"resting on that inode does not count (AGENT-BACKUP-RESTORE-"
            f"A2R4). Refusing to snapshot an unverified connection (fail "
            f"closed, never silently trusted)."
        )

    return conn


def _snapshot_sqlite(source_path: str, staged_path: str,
                      canonical_root_real: str | None = None, credentials_identity=None,
                      expected_identity=None,
                      timeout_seconds: float = _SQLITE_BACKUP_TIMEOUT_SECONDS,
                      budget: "_AggregateBudget | None" = None) -> bytes:
    """A real point-in-time-consistent copy via sqlite3.Connection.backup()
    (SQLite's own Online Backup API), not a raw file copy after a
    checkpoint. wal_checkpoint(TRUNCATE) is NOT quiescence: a checkpoint
    can report busy without raising, and even a successful checkpoint
    says nothing about a writer committing new rows in the gap between
    the checkpoint and a subsequent plain file copy -- both are exactly
    what AGENT-BACKUP-RESTORE-A2R reproduced (a 1-row source backed up as
    a 0-row archive that still verified valid=True).

    canonical_root_real/credentials_identity are optional so this
    function's lock-contention/timeout behavior stays directly testable
    without constructing boundary-check machinery; production callers
    (_collect_database_members) always pass real values. When given, the
    LIVE connection itself is bound to expected_identity via
    _connect_sqlite_with_bound_identity() (/proc/self/fd inspection of
    the connection's own actually-open descriptor, never a path-based
    re-stat) -- AGENT-BACKUP-RESTORE-A2R3 proved a path-based re-check
    (this function's previous defense here) can be defeated by swapping
    the path during connect()'s own internal open and restoring it
    before any pathname-based check runs afterward; see
    _connect_sqlite_with_bound_identity's docstring for the full
    reproduction and why fd-identity, not path-identity, is the only
    thing that actually closes this gap.

    timeout_seconds is passed to sqlite3.connect() as the connection's
    OWN busy_timeout -- without this, Python's sqlite3 module defaults
    to a 5-second busy_timeout regardless of the deadline this function
    is documented to honor, which is exactly AGENT-BACKUP-RESTORE-A2R2's
    "configured timeout = 0.25s, actual block ~= 5.01s" finding: the
    connect() call's own internal busy-handler retried for the stdlib
    default, never even reaching the code that would check our deadline.

    AGENT-BACKUP-RESTORE-A2R9 (Section 8-10, BLOCKER): returns the exact
    checkpointed, integrity-checked bytes via sqlite3.Connection.serialize()
    -- called on `dest_conn` itself, the SAME live connection this
    function just backed up into and checkpointed, BEFORE that connection
    is ever closed. A2R9 proved the OLD design (returning nothing, forcing
    every caller to reopen `staged_path` by name afterward for metadata,
    hashing, and archiving -- THREE separate reopens of one mutable
    pathname) let a same-UID, same-process substitution of `staged_path`
    between any two of those reopens silently redefine the archived
    member's content while its manifest hash still matched a DIFFERENT
    read. serialize() is source-vetted (empirically, not merely assumed
    available) to return bytes byte-for-byte identical to what is
    physically on disk at `staged_path` at the moment it is called; from
    that moment on, `staged_path` is transport/cache only -- callers must
    never reread it to determine payload bytes, a hash, a size, or
    manifest/ZIP content (Section 4/11). A `journal_mode=DELETE` pragma
    is required immediately before serialize(): empirically confirmed
    (not assumed) that a destination connection left in 'wal' journal
    mode after wal_checkpoint(TRUNCATE) -- TRUNCATE empties the WAL file,
    it does not reset the connection's own journal_mode -- serializes
    correctly but produces bytes an in-memory sqlite3 connection later
    fails to deserialize (OperationalError: unable to open database file,
    since a WAL-mode header implies sidecar -wal/-shm files an in-memory
    database has no real path to provide); switching to a non-WAL journal
    mode first avoids this without changing the checkpointed page content
    at all (verified: the resulting bytes are unchanged from what
    wal_checkpoint(TRUNCATE) alone would have produced on disk).

    Raises AgentBackupError on: source missing, connect-time busy-timeout
    exhausted, a detected identity swap, backup timeout, any other SQLite
    error, or a failed post-snapshot integrity_check on the STAGED copy."""
    if not os.path.exists(source_path):
        raise AgentBackupError(f"required database does not exist: {source_path}")

    start = time.monotonic()

    def _progress(status, remaining, total):
        if time.monotonic() - start > timeout_seconds:
            raise _SnapshotDeadline(
                f"snapshot of {source_path} exceeded {timeout_seconds}s "
                f"({remaining}/{total} pages remaining) -- sustained lock contention"
            )

    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
    if os.path.exists(staged_path):
        os.remove(staged_path)

    def _open_source():
        return sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=timeout_seconds)

    try:
        if canonical_root_real is not None:
            source_conn = _connect_sqlite_with_bound_identity(
                _open_source, source_path, expected_identity, credentials_identity)
        else:
            source_conn = _open_source()
    except sqlite3.OperationalError as e:
        raise AgentBackupError(
            f"could not open {source_path} for snapshot within {timeout_seconds}s "
            f"(SQLite busy-timeout exhausted, source is locked): {e}"
        )
    except sqlite3.Error as e:
        raise AgentBackupError(f"could not open {source_path} for snapshot: {e}")

    captured_data = None
    try:
        dest_conn = sqlite3.connect(staged_path, timeout=timeout_seconds)
        try:
            try:
                source_conn.backup(dest_conn, pages=64, progress=_progress, sleep=0.1)
            except _SnapshotDeadline as e:
                raise AgentBackupError(str(e))
            except sqlite3.OperationalError as e:
                raise AgentBackupError(
                    f"SQLite reported busy/locked in a way that defeated a consistent "
                    f"snapshot of {source_path}: {e}"
                )
            except sqlite3.Error as e:
                raise AgentBackupError(f"snapshot of {source_path} failed: {e}")

            # Fold the destination's own WAL into the single staged file --
            # exactly one physical file per database member, no sidecar
            # -wal/-shm ever enters the archive.
            try:
                dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as e:
                raise AgentBackupError(f"could not finalize staged snapshot of {source_path}: {e}")

            integrity = dest_conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise AgentBackupError(
                    f"staged snapshot of {source_path} failed integrity_check: {integrity}"
                )

            # AGENT-BACKUP-RESTORE-A2R9 (Section 8-10): capture the exact
            # bytes from THIS SAME live, just-checkpointed, just-validated
            # connection -- see this function's own docstring for why
            # journal_mode must be switched off WAL first.
            try:
                dest_conn.execute("PRAGMA journal_mode=DELETE")
                captured_data = bytes(dest_conn.serialize())
            except sqlite3.Error as e:
                raise AgentBackupError(
                    f"could not capture the exact checkpointed bytes of the staged "
                    f"snapshot of {source_path}: {e}"
                )
        finally:
            dest_conn.close()
    finally:
        source_conn.close()

    for sidecar in (staged_path + "-wal", staged_path + "-shm"):
        if os.path.exists(sidecar) and os.path.getsize(sidecar) > 0:
            raise AgentBackupError(
                f"staged snapshot of {source_path} left an un-checkpointed sidecar file: {sidecar}"
            )

    # AGENT-BACKUP-RESTORE-A2F10 (HIGH H2, Section 23): a serialized
    # SQLite snapshot counts toward the SAME aggregate resource budget
    # as any other captured member -- no database exemption. Charged
    # here, immediately after the exact bytes ultimately archived are
    # produced, exactly like every physical-file capture (Section 22).
    if budget is not None:
        budget.charge(len(captured_data), f"SQLite snapshot of {source_path}")

    return captured_data


def _sqlite_metadata(path: str) -> dict:
    """Read-only inspection of the STAGED copy via a URI-mode connection
    (mode=ro) -- never core.db.connect(), never touches journal mode."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = conn.cursor()
            version = cur.execute("SELECT sqlite_version()").fetchone()[0]
            journal_mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
            user_version = cur.execute("PRAGMA user_version").fetchone()[0]
            integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(
                r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            )
            return {
                "sqlite_version": version,
                "journal_mode": journal_mode,
                "user_version_pragma": user_version,
                "integrity_check": integrity,
                "tables": tables,
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


def _sqlite_metadata_from_bytes(data: bytes) -> dict:
    """AGENT-BACKUP-RESTORE-A2R9 (Section 8-10): the bytes-based sibling
    of _sqlite_metadata -- inspects `data` (the exact immutable bytes
    _snapshot_sqlite already captured via serialize()) through a fresh
    in-memory deserialize(), never by reopening a staging pathname a
    second time by name. _collect_database_members uses this one; the
    path-based _sqlite_metadata above is kept for any caller that
    genuinely wants to inspect an on-disk SQLite file directly (outside a
    build), not for this module's own authoritative build-time pipeline."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.deserialize(data)
            cur = conn.cursor()
            version = cur.execute("SELECT sqlite_version()").fetchone()[0]
            journal_mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
            user_version = cur.execute("PRAGMA user_version").fetchone()[0]
            integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
            tables = sorted(
                r[0] for r in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            )
            return {
                "sqlite_version": version,
                "journal_mode": journal_mode,
                "user_version_pragma": user_version,
                "integrity_check": integrity,
                "tables": tables,
            }
        finally:
            conn.close()
    except Exception as e:
        return {"error": str(e)}


def _flight_recorder_retention_metadata() -> dict:
    """Source-backed, not fabricated -- reads the real module constants
    rather than inventing plausible-looking numbers."""
    try:
        from core import flight_recorder as fr
        return {
            "full_retention_seconds": fr.FULL_RETENTION_SECONDS,
            "error_retention_seconds": fr.ERROR_RETENTION_SECONDS,
            "max_db_bytes": fr.DEFAULT_MAX_DB_BYTES,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------
# Baseline provenance (Section 10) -- generation is a release-time act,
# never something build_agent_backup performs against the live tree it
# is currently backing up. Hashes come from immutable GIT OBJECTS at a
# specific commit, never from working-tree bytes.
# ---------------------------------------------------------------------

def _git_show_bytes(base_dir: str, ref: str, rel_path: str):
    """Returns (ok, bytes_or_None, reason). Reads rel_path's blob bytes at
    `ref` (a commit-ish) via `git show`, never working-tree bytes."""
    try:
        show = subprocess.run(
            ["git", "-C", base_dir, "show", f"{ref}:{rel_path}"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, None, f"could not read {rel_path!r} from {ref}: {e}"
    if show.returncode != 0:
        return False, None, f"{rel_path!r} does not exist at {ref}"
    return True, show.stdout, ""


def _verify_baseline_against_git(base_dir: str, git_commit, files: dict) -> tuple[bool, str]:
    """Returns (ok, reason). ok=True only if base_dir is a git checkout,
    git_commit resolves to an actual commit reachable in it, every path
    in `files` falls inside the allowed shipped-baseline category
    prefixes (_BASELINE_CATEGORIES), every stored hash matches the
    ACTUAL git blob bytes at git_commit for that path, AND (AGENT-
    BACKUP-RESTORE-A2R6 model, see below) every path's blob at the
    checkout's CURRENT HEAD still matches that same declared hash.

    AGENT-BACKUP-RESTORE-A2R3: a forged metadata file with a nonexistent
    git_commit and attacker-selected hashes was trusted outright by the
    previous _load_baseline_hashes. Closed by requiring git_commit to
    resolve to a real commit and every hash to match real git object
    bytes at that commit -- re-derived independently, never trusting the
    artifact's own self-declared hash map.

    AGENT-BACKUP-RESTORE-A2R4 (R4-H1): the checks above alone only
    proved git_commit resolves to SOME real, self-consistent commit --
    never that it was the checkout's CURRENT release identity. A2F4's
    first attempted fix required git_commit == current HEAD exactly.

    AGENT-BACKUP-RESTORE-A2R5 (PRIORITY ZERO): A2F4's exact-HEAD fix is
    structurally impossible to satisfy the moment the baseline artifact
    is itself git-tracked -- a commit's SHA is a hash of its tree, which
    includes the artifact's own bytes, so the artifact can never
    correctly declare the SHA of the commit that contains it (a genuine
    fixed-point impossibility, not an engineering gap). Proven live: a
    disposable-repo simulation of the actual release-checkpoint
    lifecycle showed the artifact fails authentication the INSTANT it is
    committed, and a read-only check against the real ~/lumina dev
    checkout showed this was ALREADY happening today, pre-checkpoint,
    for the documented true-ancestry mirror workflow. A2R5 additionally
    warned that a bare `git merge-base --is-ancestor` relaxation (fixing
    the fixed-point problem) would let an arbitrarily stale same-version
    baseline stay trusted forever, even after a later, legitimate commit
    changes the very files the baseline describes.

    AGENT-BACKUP-RESTORE-A2F5 model (Sol's specification, Section 1-2):
    git_commit is not 'the commit currently running' -- it is the
    PROVENANCE ANCHOR from which the baseline's hash entries were
    derived. Trust requires ALL of:
      1. git_commit resolves to a real commit.
      2. git_commit is an ANCESTOR of (or equal to) the checkout's
         current HEAD -- true for a legitimate release checkpoint that
         commits the artifact alongside itself (the artifact's own
         commit is necessarily a descendant of the anchor), and true for
         a true-ancestry dev-local mirror descending from the release
         commit.
      3. lumina_version matches (checked by the caller, _load_baseline_
         hashes, before this function even runs).
      4. every declared path's blob at git_commit matches the declared
         hash (closes A2R3's fabrication case).
      5. every declared path's blob at CURRENT HEAD *also* matches the
         declared hash (closes A2R5's stale-ancestor case: if a later,
         legitimate commit changed a baseline-owned file, its current-
         HEAD blob no longer matches what the baseline (correctly, for
         the OLD commit) declared, and the whole baseline is rejected
         as stale -- not merely that one entry).
    No git fixed point is required: the anchor commit is free to be an
    ANCESTOR, never required to be HEAD itself, so a release checkpoint
    (whose own commit necessarily post-dates and descends from whatever
    anchor it declares) and ordinary forward development or a dev-local
    mirror can never structurally break this by existing.

    Working-tree bytes are never consulted for either step 4 or step 5
    -- both read committed git objects via `git show <ref>:<path>`, so a
    dirty working tree cannot redefine or invalidate an otherwise-valid
    baseline (Section 2's explicit requirement)."""
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return False, "base_dir is not a git checkout -- no immutable build identity to verify the baseline against"
    if not isinstance(git_commit, str) or not git_commit:
        return False, f"baseline git_commit is missing or not a string: {git_commit!r}"

    try:
        check = subprocess.run(
            ["git", "-C", base_dir, "cat-file", "-e", f"{git_commit}^{{commit}}"],
            capture_output=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"could not invoke git to verify baseline commit {git_commit!r}: {e}"
    if check.returncode != 0:
        return False, f"baseline git_commit {git_commit!r} does not resolve to a real commit in this checkout"

    try:
        head = subprocess.run(
            ["git", "-C", base_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"could not resolve this checkout's current HEAD: {e}"
    if head.returncode != 0:
        return False, "could not resolve this checkout's current HEAD (git rev-parse HEAD failed)"
    current_head = head.stdout.strip()

    try:
        ancestor_check = subprocess.run(
            ["git", "-C", base_dir, "merge-base", "--is-ancestor", git_commit, current_head],
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"could not verify baseline commit ancestry: {e}"
    if ancestor_check.returncode != 0:
        return False, (
            f"baseline git_commit {git_commit!r} is a real commit but is not an "
            f"ancestor of (or equal to) this checkout's current HEAD {current_head!r} "
            f"-- a provenance anchor from an unrelated line of history is not "
            f"authoritative for what THIS release currently ships"
        )

    for rel_path, claimed_hash in files.items():
        if not isinstance(rel_path, str) or not isinstance(claimed_hash, str):
            return False, f"baseline files map has a non-string path or hash: {rel_path!r}: {claimed_hash!r}"
        norm = rel_path.replace("\\", "/")
        if not any(norm == cat or norm.startswith(cat + "/") for cat in _BASELINE_CATEGORIES):
            return False, f"baseline claims a path outside the allowed shipped-overlay namespace: {rel_path!r}"

        ok, data, reason = _git_show_bytes(base_dir, git_commit, rel_path)
        if not ok:
            return False, f"baseline path {rel_path!r} does not exist at anchor commit {git_commit} -- fabricated entry ({reason})"
        if hashlib.sha256(data).hexdigest() != claimed_hash:
            return False, (
                f"baseline hash for {rel_path!r} does not match the actual git blob "
                f"at anchor commit {git_commit} -- tampered baseline entry"
            )

        ok, data, reason = _git_show_bytes(base_dir, current_head, rel_path)
        if not ok:
            return False, (
                f"baseline-owned path {rel_path!r} no longer exists at this checkout's "
                f"current HEAD {current_head!r} -- stale baseline, not authoritative "
                f"for what THIS release currently ships ({reason})"
            )
        if hashlib.sha256(data).hexdigest() != claimed_hash:
            return False, (
                f"baseline-owned path {rel_path!r} has changed between anchor commit "
                f"{git_commit!r} and this checkout's current HEAD {current_head!r} -- "
                f"stale baseline entry, a later legitimate commit superseded it"
            )

    return True, ""


def _load_baseline_hashes(path: str | None, expected_lumina_version: str | None = None,
                           base_dir: str | None = None):
    """Standalone, path-based entry point: opens `path` once and delegates
    parsing/authentication to _parse_baseline_hashes. Used by callers (and
    tests) that want to load/inspect a baseline file on its own, outside a
    build. AGENT-BACKUP-RESTORE-A2R8/A2F8 (BLOCKER B2): build_agent_backup
    itself no longer calls this function -- a build captures shipped_
    baseline_hashes.json's bytes exactly ONCE (via
    _capture_and_stage_physical_state_file, for the archived payload) and
    feeds those SAME bytes directly to _parse_baseline_hashes, never
    reopening baseline_hashes_path a second time by name for the
    provenance-control decision. See _parse_baseline_hashes for the actual
    parsing/authentication logic and _collect_metadata_members /
    _build_agent_backup_core for the single-capture build-time path.

    Returns (files_dict, warning_or_None). Never raises."""
    if not path or not os.path.exists(path):
        return {}, None  # normal case: no baseline configured at all -- the caller's
                          # existing generic "no baseline_hashes_path supplied" warning covers this
    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
    except OSError as e:
        return {}, (
            f"baseline_hashes_path {path!r} could not be read/parsed ({e}) -- "
            f"provenance defaults to 'unknown'"
        )
    return _parse_baseline_hashes(raw_bytes, expected_lumina_version, base_dir, source_label=path)


def _parse_baseline_hashes(raw_bytes: bytes, expected_lumina_version: str | None,
                            base_dir: str | None, source_label: str):
    """The parsing/authentication half of baseline-hash loading, operating
    on already-captured bytes rather than a filesystem path -- factored out
    of _load_baseline_hashes (AGENT-BACKUP-RESTORE-A2R8, Section 7-11,
    BLOCKER B2) so a build can feed this the EXACT SAME bytes
    _collect_metadata_members already captured (once) and archived, never
    a second, separate open of the live baseline_hashes_path for the
    provenance-control decision. `source_label` is used only for error
    messages (the path the bytes were captured from, or another
    descriptive label) -- it is never itself reopened or read by this
    function.

    Returns (files_dict, warning_or_None). Never raises; never treats a
    structurally-wrong, wrong-format, or version-mismatched baseline as
    authoritative -- falls back to an empty files_dict plus an explicit
    warning instead (Section 10: 'use truthful unknown + warning if no
    valid matching baseline is available; never silently treat a
    stale/wrong-release baseline as authoritative'). A pre-A2F2 flat
    {path: hash} baseline (no format/version/commit binding at all) is
    treated the same way -- untrusted, not a silent special case.

    base_dir, when given, additionally cross-checks the artifact's own
    git_commit/files claims against real git object bytes via
    _verify_baseline_against_git -- AGENT-BACKUP-RESTORE-A2R3's forged-
    baseline finding: a baseline that merely PARSES correctly and claims
    a plausible-looking format/version/lumina_version is not the same as
    one that is actually true. When base_dir is not given (or isn't a
    git checkout), this falls back to the pre-git-verification structural
    checks only -- honest 'unknown', not invented trust."""
    try:
        data = _strict_json_loads(raw_bytes)
    except Exception as e:
        return {}, (
            f"baseline_hashes_path {source_label!r} could not be read/parsed ({e}) -- "
            f"provenance defaults to 'unknown'"
        )

    if not isinstance(data, dict):
        return {}, f"baseline_hashes_path {source_label!r} does not contain a JSON object -- provenance defaults to 'unknown'"

    if "files" not in data:
        return {}, (
            f"baseline_hashes_path {source_label!r} is in the legacy flat format with no "
            f"format/version/commit binding -- treated as unknown/stale, not authoritative"
        )

    if data.get("format") != BASELINE_FORMAT:
        return {}, (
            f"baseline_hashes_path {source_label!r} has unexpected format "
            f"{data.get('format')!r} -- provenance defaults to 'unknown'"
        )

    version = data.get("format_version")
    if not isinstance(version, dict) or isinstance(version.get("major"), bool) \
            or not isinstance(version.get("major"), int) \
            or version["major"] != BASELINE_FORMAT_VERSION["major"]:
        return {}, (
            f"baseline_hashes_path {source_label!r} has an unsupported format_version "
            f"{version!r} -- provenance defaults to 'unknown'"
        )

    if data.get("hash_algorithm") != "sha256":
        return {}, (
            f"baseline_hashes_path {source_label!r} has an unsupported hash_algorithm "
            f"{data.get('hash_algorithm')!r} -- provenance defaults to 'unknown'"
        )

    files = data.get("files")
    if not isinstance(files, dict):
        return {}, f"baseline_hashes_path {source_label!r} is missing a valid 'files' object -- provenance defaults to 'unknown'"

    baseline_version = data.get("lumina_version")
    if expected_lumina_version is not None and baseline_version not in (expected_lumina_version, "unknown"):
        return {}, (
            f"baseline_hashes_path {source_label!r} was generated for lumina_version "
            f"{baseline_version!r}, not the running {expected_lumina_version!r} -- "
            f"treated as a stale/wrong-release baseline, provenance defaults to 'unknown'"
        )

    if base_dir is not None:
        ok, reason = _verify_baseline_against_git(base_dir, data.get("git_commit"), files)
        if not ok:
            return {}, (
                f"baseline_hashes_path {source_label!r} could not be authenticated against "
                f"this checkout's git objects ({reason}) -- provenance defaults to "
                f"'unknown' rather than trusting the artifact's own self-declared "
                f"commit/hash claims"
            )

    return files, None


def _classify_provenance(baseline_key: str, sha256: str, baseline: dict) -> str:
    if baseline_key not in baseline:
        return "unknown"
    return "shipped_baseline_unmodified" if baseline[baseline_key] == sha256 else "shipped_baseline_modified"


def _default_baseline_hashes_path(base_dir: str) -> str:
    return os.path.join(base_dir, "metadata", "shipped_baseline_hashes.json")


def generate_baseline_hashes(base_dir: str, commit: str = "HEAD") -> dict:
    """Computes the shipped-baseline hash reference set from IMMUTABLE GIT
    OBJECT bytes at `commit` (default HEAD) -- never from working-tree
    bytes. AGENT-BACKUP-RESTORE-A2R2 found the previous generator listed
    git-TRACKED file NAMES via `git ls-files` but then hashed whatever was
    currently on disk via _sha256_file(): a locally-dirty tracked file was
    therefore falsely canonized as "shipped" truth. `git show
    <commit>:<path>` instead reads each blob exactly as committed,
    regardless of what the working tree currently contains.

    Returns {} (never fabricates a baseline) if base_dir isn't a git
    checkout, git is unavailable, or `commit` doesn't resolve. On success
    returns a version-bound dict: {format, format_version, lumina_version,
    git_commit, hash_algorithm, files}. Intended to run ONCE per release,
    against a clean checkout at the release commit, with the result
    committed as metadata/shipped_baseline_hashes.json and then only ever
    READ (via _load_baseline_hashes) by build_agent_backup at backup time."""
    try:
        rev_parse = subprocess.run(
            ["git", "-C", base_dir, "rev-parse", commit],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if rev_parse.returncode != 0:
        return {}
    resolved_commit = rev_parse.stdout.strip()

    try:
        result = subprocess.run(
            ["git", "-C", base_dir, "ls-tree", "-r", "--name-only", resolved_commit, "--"]
            + _BASELINE_CATEGORIES,
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if result.returncode != 0:
        return {}

    files = {}
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        try:
            show = subprocess.run(
                ["git", "-C", base_dir, "show", f"{resolved_commit}:{rel}"],
                capture_output=True, timeout=10,
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if show.returncode != 0:
            continue
        files[rel.replace(os.sep, "/")] = hashlib.sha256(show.stdout).hexdigest()

    return {
        "format": BASELINE_FORMAT,
        "format_version": BASELINE_FORMAT_VERSION,
        "lumina_version": _read_lumina_version(base_dir),
        "git_commit": resolved_commit,
        "hash_algorithm": "sha256",
        "files": files,
    }


# ---------------------------------------------------------------------
# Member collection -- returns member dicts with staged_path (never the
# original source path) plus archive metadata.
# ---------------------------------------------------------------------

def _require_immediate_sqlite_validity(data: bytes, role: str, source_path: str) -> None:
    """AGENT-BACKUP-RESTORE-A2R9 (Section 9, mandatory property): role/
    schema/quick_check validation must apply to the exact immutable byte
    object that is ultimately archived -- and it must apply IMMEDIATELY,
    at collection time, not only later during the pre-publish
    verify_agent_backup pass on the finished archive. This is fail-fast,
    not a substitute for that later independent re-verification (which
    still runs unconditionally, against the finished archive's own
    bytes, exactly as before) -- it exists so a snapshot that captured
    invalid bytes for some reason fails loudly and immediately, at the
    collector that produced them, rather than surfacing later as an
    opaque whole-archive verification failure."""
    ok, reason = _verify_sqlite_payload(data, role=role)
    if not ok:
        raise AgentBackupError(
            f"snapshot of {source_path} produced bytes that failed immediate "
            f"exact-byte validation ({reason}) -- refusing to archive an "
            f"unvalidated SQLite snapshot"
        )


def _collect_database_members(data_dir: str, staging_root: str, canonical_data_root: str,
                                credentials_identity, forbidden_identities: dict,
                                warnings: list, budget: "_AggregateBudget | None" = None) -> list[dict]:
    """credentials_identity (bare) is threaded through unchanged to
    _snapshot_sqlite/_connect_sqlite_with_bound_identity -- their
    connection-bound fd-identity mechanism (AGENT-BACKUP-RESTORE-A2R4's
    R4-B1 repair, independently re-cleared by A2R5) is deliberately NOT
    touched by A2F5 (Section 21/22: 'preserve R5 PASS areas... no need
    to redesign the already-cleared SQLite snapshot mechanism'). The
    pre-connect trust-boundary pre-check below uses the wider
    forbidden_identities set (credentials + ledger, Section 9) since it
    goes through the same _open_verified _verify_sqlite_source_identity
    always used -- ledger.db can never legitimately masquerade as
    lumina.db/flight_recorder.db's role anyway (caught by the schema-
    fingerprint check at verify time regardless), but the identity is
    checked here too for defense-in-depth and consistency."""
    members = []

    lumina_db = os.path.join(data_dir, "memory", "lumina.db")
    if not os.path.exists(lumina_db):
        raise AgentBackupError(
            f"required primary database not found: {lumina_db} -- refusing to "
            f"produce a backup with no primary database"
        )
    ok, reason, existence_failure, identity = _verify_sqlite_source_identity(
        lumina_db, canonical_data_root, forbidden_identities)
    if not ok:
        if existence_failure:
            raise AgentBackupError(
                f"required primary database disappeared or became inaccessible "
                f"during staging ({reason}): {lumina_db}"
            )
        raise AgentBackupError(
            f"required primary database failed its trust-boundary check "
            f"({reason}): {lumina_db} -- refusing to snapshot a source that "
            f"aliases the credentials store or escapes its canonical root"
        )
    staged = os.path.join(staging_root, "state", "databases", "lumina.db")
    data = _snapshot_sqlite(lumina_db, staged, canonical_data_root, credentials_identity, identity,
                             budget=budget)
    _require_immediate_sqlite_validity(data, "lumina_db", lumina_db)
    members.append({
        "logical_id": "state.databases.lumina_db",
        "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database",
        "archive_path": "state/databases/lumina.db",
        "staged_path": staged,
        "data": data,
        "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "overwrite_atomic_then_rewrite_skills_path_column",
        "rebind_policy": "rewrite_skills_path_column_to_dest_base_dir",
        "database_metadata": _sqlite_metadata_from_bytes(data),
    })

    telemetry_db = os.path.join(data_dir, "telemetry", "flight_recorder.db")
    if not os.path.exists(telemetry_db):
        raise AgentBackupError(
            f"required Flight Recorder database not found: {telemetry_db} -- "
            f"Flight Recorder is REQUIRED_AGENT_STATE per the A1 contract "
            f"(AGENT_BACKUP_RESTORE_A1_DESIGN_2026-09-08.md Section C), refusing "
            f"to produce an incomplete backup"
        )
    ok, reason, existence_failure, identity = _verify_sqlite_source_identity(
        telemetry_db, canonical_data_root, forbidden_identities)
    if not ok:
        if existence_failure:
            raise AgentBackupError(
                f"required Flight Recorder database disappeared or became "
                f"inaccessible during staging ({reason}): {telemetry_db}"
            )
        raise AgentBackupError(
            f"required Flight Recorder database failed its trust-boundary "
            f"check ({reason}): {telemetry_db}"
        )
    staged = os.path.join(staging_root, "state", "telemetry", "flight_recorder.db")
    data = _snapshot_sqlite(telemetry_db, staged, canonical_data_root, credentials_identity, identity,
                             budget=budget)
    _require_immediate_sqlite_validity(data, "flight_recorder_db", telemetry_db)
    db_meta = _sqlite_metadata_from_bytes(data)
    retention = _flight_recorder_retention_metadata()
    if retention:
        db_meta["retention_policy"] = retention
    members.append({
        "logical_id": "state.telemetry.flight_recorder_db",
        "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "sqlite_database",
        "archive_path": "state/telemetry/flight_recorder.db",
        "staged_path": staged,
        "data": data,
        "required": True,
        "portable": "PORTABLE",
        "restore_policy": "overwrite_atomic",
        "database_metadata": db_meta,
    })
    # ledger.db: never staged, never snapshotted, never referenced here.

    return members


def _collect_preference_members(data_dir: str, staging_root: str,
                                 canonical_data_root: str, forbidden_identities: dict,
                                 warnings: list, budget: "_AggregateBudget | None" = None) -> list[dict]:
    prefs_path = os.path.join(data_dir, "memory", "prefs.json")
    archive_path = "state/preferences/prefs.json"
    data, staged = _stage_physical_state_file(prefs_path, staging_root, archive_path,
                                               canonical_data_root, forbidden_identities,
                                               "required preferences file (prefs.json)", warnings,
                                               budget=budget)
    if staged is None:
        raise AgentBackupError(
            f"required preferences file not found: {prefs_path} -- refusing to "
            f"produce an incomplete backup"
        )
    return [{
        "logical_id": "state.preferences.prefs_json",
        "state_class": "REQUIRED_AGENT_STATE",
        "content_kind": "json",
        "archive_path": archive_path,
        "staged_path": staged,
        "data": data,
        "required": True,
        "portable": "PORTABLE_WITH_REBIND",
        "restore_policy": "overwrite_then_rewrite_last_persona_path",
        "rebind_policy": "rewrite_last_persona_absolute_path_to_dest_personas_dir",
    }]


def _collect_custom_tool_members(data_dir: str, staging_root: str,
                                  forbidden_identities: dict, warnings: list,
                                  budget: "_AggregateBudget | None" = None) -> list[dict]:
    custom_tools_dir = os.path.join(data_dir, "custom_tools")
    root_real = _require_readable_root(custom_tools_dir, "custom_tools", warnings)
    if root_real is None:
        return []  # VALIDLY_ABSENT, or a rejected/unreadable root already warned/raised above

    members = []
    for root, dirs, files in _safe_walk(custom_tools_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and
                   not os.path.islink(os.path.join(root, d))]
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, custom_tools_dir).replace(os.sep, "/")
            archive_path = f"state/custom_tools/{rel}"
            data, staged = _stage_physical_state_file(src, staging_root, archive_path,
                                                       root_real, forbidden_identities,
                                                       f"required custom-tool source ({rel})", warnings,
                                                       budget=budget)
            if staged is None:
                # _safe_walk already enumerated this exact filename as
                # present a moment ago -- a None here means it vanished
                # in the gap between enumeration and staging, not that it
                # never existed; that must escalate, never be silently
                # skipped (AGENT-BACKUP-RESTORE-A2R2's original finding).
                raise AgentBackupError(
                    f"required custom-tool source disappeared or became "
                    f"inaccessible during staging: {src}"
                )
            members.append({
                "logical_id": f"state.custom_tools.{rel}",
                "state_class": "REQUIRED_AGENT_STATE",
                "content_kind": "python_source",
                "archive_path": archive_path,
                "staged_path": staged,
                "data": data,
                "required": True,
                "portable": "PORTABLE_WITH_REBIND",
                "restore_policy": "restore_verbatim_then_quarantine_pending_reapproval",
                "rebind_policy": "quarantine_until_explicit_reapproval",
            })
    return members


def _collect_audit_members(data_dir: str, staging_root: str,
                            canonical_data_root: str, forbidden_identities: dict,
                            warnings: list, budget: "_AggregateBudget | None" = None) -> list[dict]:
    members = []
    memory_dir = os.path.join(data_dir, "memory")

    tool_audit = os.path.join(memory_dir, "tool_audit.log")
    archive_path = "state/audit/tool_audit.log"
    data, staged = _stage_physical_state_file(
        tool_audit, staging_root, archive_path, canonical_data_root, forbidden_identities,
        "tool_audit.log (required, load-bearing audit history per A1)", warnings, budget=budget)
    if staged is not None:
        members.append({
            "logical_id": "state.audit.tool_audit_log",
            "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "jsonl_log",
            "archive_path": archive_path,
            "staged_path": staged,
            "data": data,
            "required": True,
            "portable": "PORTABLE_WITH_REBIND",
            "restore_policy": (
                "restore_verbatim_historical_only_never_auto_grants_"
                "executable_approval_on_destination"
            ),
            "rebind_policy": "quarantine_until_explicit_reapproval",
        })

    for fname in ("pending_actions.json", "pending_actions_audit.log"):
        src = os.path.join(memory_dir, fname)
        archive_path = f"state/audit/{fname}"
        data, staged = _stage_physical_state_file(
            src, staging_root, archive_path, canonical_data_root, forbidden_identities,
            f"{fname} (required audit state)", warnings, budget=budget)
        if staged is None:
            continue
        members.append({
            "logical_id": f"state.audit.{fname}",
            "state_class": "REQUIRED_AGENT_STATE",
            "content_kind": "json" if fname.endswith(".json") else "jsonl_log",
            "archive_path": archive_path,
            "staged_path": staged,
            "data": data,
            "required": True,
            "portable": "PORTABLE",
            "restore_policy": "overwrite_atomic",
        })
    return members


def _collect_overlay_dir(base_dir: str, staging_root: str, rel_dir: str, archive_prefix: str,
                          keep_fn, logical_prefix: str, forbidden_identities: dict,
                          warnings: list, budget: "_AggregateBudget | None" = None) -> list[dict]:
    """AGENT-BACKUP-RESTORE-A2R5 (discovery-to-stage TOCTOU, BLOCKER):
    A2F4 checked special-file/symlink shape via a SEPARATE lstat() here,
    then staged through the lenient _stage_plain_file path afterward --
    raced by swapping the file to a symlink in the gap between the two.
    A2F5 removes the separate discovery-time shape check entirely and
    stages via _stage_physical_state_file, whose _open_state_bearing
    primitive makes the security decision from the SAME open+fstat that
    captures the bytes -- no gap to race. _safe_listdir still provides
    the file NAME (a directory listing has no atomic per-file open
    equivalent), but the trust decision about what that name currently
    IS happens only once, inside the stage call below."""
    abs_dir = os.path.join(base_dir, rel_dir)
    root_real = _require_readable_root(abs_dir, rel_dir, warnings)
    if root_real is None:
        return []  # VALIDLY_ABSENT, or a rejected/unreadable root already warned/raised above

    members = []
    for fname in _safe_listdir(abs_dir):
        if not keep_fn(fname):
            continue
        fpath = os.path.join(abs_dir, fname)
        baseline_key = "/".join(rel_dir.split(os.sep) + [fname])
        archive_path = f"{archive_prefix}/{fname}"
        data, staged = _stage_physical_state_file(
            fpath, staging_root, archive_path, root_real, forbidden_identities,
            f"required overlay source ({fpath})", warnings, budget=budget)
        if staged is None:
            # _safe_listdir already enumerated this exact filename as
            # present a moment ago -- a None here means it vanished in
            # the gap between enumeration and staging, not that it never
            # existed; that must escalate, never be silently skipped
            # (AGENT-BACKUP-RESTORE-A2R2's original finding).
            raise AgentBackupError(
                f"required overlay source disappeared or became inaccessible "
                f"during staging: {fpath}"
            )
        members.append({
            "logical_id": f"{logical_prefix}.{fname}",
            "state_class": "USER_OVERLAY",
            "content_kind": "json" if fname.endswith(".json") else (
                "markdown" if fname.endswith(".md") else "binary"),
            "archive_path": archive_path,
            "staged_path": staged,
            "data": data,
            "required": True,
            "portable": "PORTABLE",
            "restore_policy": "baseline_aware_merge",
            "_baseline_key": baseline_key,
        })
    return members


def _project_dir_names(root_real: str | None, listing_root: str, warnings: list) -> set:
    if root_real is None:
        return set()
    names = set()
    for n in _safe_listdir(listing_root):
        p = os.path.join(listing_root, n)
        try:
            lst = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISDIR(lst.st_mode) and not stat.S_ISLNK(lst.st_mode):
            names.add(n)
    return names


def _collect_project_members(data_dir: str, base_dir: str, staging_root: str,
                              forbidden_identities: dict, warnings: list,
                              budget: "_AggregateBudget | None" = None) -> list[dict]:
    """Collects Project state by logical Project IDENTITY across BOTH
    BASE_DIR/projects/<name> (portable docs) and DATA_DIR/projects/<name>
    (binding/chat linkage), rather than requiring one physical root to
    discover the other (Section 6: AGENT-BACKUP-RESTORE-A2R2 found a
    project with binding.json/chats.json in DATA_DIR but no matching
    BASE_DIR/projects/<name>/ directory was never discovered at all,
    since the previous implementation only ever enumerated the BASE_DIR
    side and looked up DATA_DIR nested inside that loop).

    projectlist.md/project.md/binding.json/chats.json are all REQUIRED,
    state-bearing (Section 5-7, A2F5) -- staged via the atomic
    _stage_physical_state_file boundary, whose 'absent' outcome IS the
    correct, valid case here (none of these are pre-discovered via a
    listdir/walk first; each is a direct, ad-hoc check of one known
    filename, so 'does not exist right now' genuinely means 'this
    project doesn't have one', not 'it vanished after being seen').
    codebase.md is REBUILDABLE_GENERATED/required=False (A1/Section 4's
    own explicit carve-out for absence) but is captured via the SAME
    _stage_physical_state_file boundary as everything else (AGENT-BACKUP-
    RESTORE-A2R7, Section 2-4: 'optional' governs absence only, never a
    weaker security boundary for present bytes -- the prior lenient
    _stage_plain_file path this used is retired entirely)."""
    members = []
    projects_base = os.path.join(base_dir, "projects")
    projects_data = os.path.join(data_dir, "projects")

    base_root_real = _require_readable_root(projects_base, "projects", warnings)

    if base_root_real is not None:
        projectlist = os.path.join(projects_base, "projectlist.md")
        archive_path = "overlay/projects/projectlist.md"
        data, staged = _stage_physical_state_file(
            projectlist, staging_root, archive_path, base_root_real, forbidden_identities,
            "projectlist.md", warnings, budget=budget)
        if staged:
            members.append({
                "logical_id": "overlay.projects.projectlist",
                "state_class": "USER_OVERLAY",
                "content_kind": "markdown",
                "archive_path": archive_path,
                "staged_path": staged,
                "data": data,
                "required": True,
                "portable": "PORTABLE",
                "restore_policy": "baseline_aware_merge",
                "_baseline_key": "projects/projectlist.md",
            })

    data_root_real = _require_readable_root(projects_data, "projects data", warnings)

    base_names = _project_dir_names(base_root_real, projects_base, warnings) if base_root_real else set()
    data_names = _project_dir_names(data_root_real, projects_data, warnings) if data_root_real else set()
    all_names = sorted(base_names | data_names)

    for name in all_names:
        if name in base_names:
            project_dir = os.path.join(projects_base, name)

            project_md = os.path.join(project_dir, "project.md")
            archive_path = f"overlay/projects/{name}/project.md"
            data, staged = _stage_physical_state_file(
                project_md, staging_root, archive_path, base_root_real, forbidden_identities,
                f"project {name}'s project.md", warnings, budget=budget)
            if staged:
                members.append({
                    "logical_id": f"overlay.projects.{name}.project_md",
                    "state_class": "USER_OVERLAY",
                    "content_kind": "markdown",
                    "archive_path": archive_path,
                    "staged_path": staged,
                    "data": data,
                    "required": True,
                    "portable": "PORTABLE",
                    "restore_policy": "overwrite_atomic",
                })

            codebase_md = os.path.join(project_dir, "codebase.md")
            archive_path = f"overlay/projects/{name}/codebase.md"
            data, staged = _stage_physical_state_file(
                codebase_md, staging_root, archive_path, base_root_real, forbidden_identities,
                f"project {name}'s codebase.md (optional, rebuildable)", warnings, budget=budget)
            if staged:
                    members.append({
                        "logical_id": f"overlay.projects.{name}.codebase_md",
                        "state_class": "REBUILDABLE_GENERATED",
                        "content_kind": "markdown",
                        "archive_path": archive_path,
                        "staged_path": staged,
                        "data": data,
                        "required": False,
                        "portable": "REBUILD",
                        "restore_policy": "restore_then_mark_stale_recommend_regenerate",
                    })

        if name in data_names:
            binding_json = os.path.join(projects_data, name, "binding.json")
            archive_path = f"overlay/projects/{name}/binding.json"
            data, staged = _stage_physical_state_file(
                binding_json, staging_root, archive_path, data_root_real, forbidden_identities,
                f"project {name}'s binding.json", warnings, budget=budget)
            if staged:
                members.append({
                    "logical_id": f"overlay.projects.{name}.binding_json",
                    "state_class": "REBIND_REQUIRED",
                    "content_kind": "json",
                    "archive_path": archive_path,
                    "staged_path": staged,
                    "data": data,
                    "required": True,
                    "portable": "PORTABLE_WITH_REBIND",
                    "restore_policy": "restore_then_require_owner_confirmation_of_root_path",
                    "rebind_policy": "validate_root_exists_or_prompt_owner_to_repick",
                })

            chats_json = os.path.join(projects_data, name, "chats.json")
            archive_path = f"overlay/projects/{name}/chats.json"
            data, staged = _stage_physical_state_file(
                chats_json, staging_root, archive_path, data_root_real, forbidden_identities,
                f"project {name}'s chats.json", warnings, budget=budget)
            if staged:
                members.append({
                    "logical_id": f"overlay.projects.{name}.chats_json",
                    "state_class": "REQUIRED_AGENT_STATE",
                    "content_kind": "json",
                    "archive_path": archive_path,
                    "staged_path": staged,
                    "data": data,
                    "required": True,
                    "portable": "PORTABLE",
                    "restore_policy": "overwrite_atomic_with_lumina_db",
                })

    return members


def _collect_metadata_members(staging_root: str, identity_trailers_path: str | None,
                               baseline_hashes_path: str | None,
                               forbidden_identities: dict, warnings: list,
                               budget: "_AggregateBudget | None" = None):
    """identity_trailers.json/shipped_baseline_hashes.json are both
    required=False, operator/release metadata -- NOT part of A1's
    REQUIRED_AGENT_STATE matrix (Section 4/7) -- so absence is fine and
    never escalates. AGENT-BACKUP-RESTORE-A2R7 (Section 2-4, BLOCKER):
    'required=False' governs absence ONLY -- when either file IS
    present, it goes through the exact same universal physical-file
    capture boundary (_stage_physical_state_file) as every REQUIRED_
    AGENT_STATE member. The prior lenient _stage_plain_file path (no
    nlink check, no stability proof, a single unguarded read) is
    retired entirely, along with the discovery-time os.path.isfile/
    os.path.islink pre-checks that used to gate it -- both were
    redundant with (and, for islink, a non-atomic shadow of) the atomic
    O_NOFOLLOW-based decision _open_state_bearing already makes from the
    opened descriptor itself.

    AGENT-BACKUP-RESTORE-A2R9 (Section 2-8): every physical member now
    returns its captured bytes from _stage_physical_state_file (the
    formerly-separate dual-use _capture_and_stage_physical_state_file has
    been merged into it -- see that function's own docstring), including
    shipped_baseline_hashes.json, this module's one confirmed dual-use
    physical member (archived AND consulted for a build-time provenance
    decision) -- its exact captured bytes are returned alongside the
    staged path for the caller to reuse, never reopened a second time by
    name for that decision.

    Returns (members: list[dict], baseline_snapshot_data: bytes | None) --
    the latter is the EXACT bytes captured for shipped_baseline_hashes.json
    (or None if it is genuinely absent, unconfigured, or its parent
    directory doesn't pass the root trust check), for
    _build_agent_backup_core to feed directly to _parse_baseline_hashes."""
    members = []
    baseline_snapshot_data = None

    identity_path = identity_trailers_path or os.path.expanduser("~/.config/lumina/identity_trailers.json")
    parent_real = _validate_root(os.path.dirname(identity_path))
    if parent_real is not None:
        archive_path = "metadata/identity_trailers.json"
        data, staged = _stage_physical_state_file(
            identity_path, staging_root, archive_path, parent_real, forbidden_identities,
            "identity_trailers.json (optional operator metadata)", warnings, budget=budget)
        if staged:
            members.append({
                "logical_id": "metadata.identity_trailers",
                "state_class": "USER_OVERLAY",
                "content_kind": "json",
                "archive_path": archive_path,
                "staged_path": staged,
                "data": data,
                "required": False,
                "portable": "PORTABLE",
                "provenance": "operator_authored",
                "non_runtime_authority": True,
                "restore_policy": "restore_verbatim_if_present_never_required_never_credentials",
            })

    if baseline_hashes_path:
        parent_real = _validate_root(os.path.dirname(baseline_hashes_path))
        if parent_real is not None:
            archive_path = "metadata/shipped_baseline_hashes.json"
            baseline_snapshot_data, staged = _stage_physical_state_file(
                baseline_hashes_path, staging_root, archive_path, parent_real, forbidden_identities,
                "shipped_baseline_hashes.json (optional release metadata)", warnings, budget=budget)
            if staged:
                members.append({
                    "logical_id": "metadata.shipped_baseline_hashes",
                    "state_class": "USER_OVERLAY",
                    "content_kind": "json",
                    "archive_path": archive_path,
                    "staged_path": staged,
                    "data": baseline_snapshot_data,
                    "required": False,
                    "portable": "PORTABLE",
                    "restore_policy": "restore_verbatim_if_present_informational_only",
                    "note": "release-time provenance-classification reference, not agent state itself",
                })

    return members, baseline_snapshot_data


def _collect_members(data_dir: str, base_dir: str, staging_root: str,
                      identity_trailers_path: str | None, baseline_hashes_path: str | None,
                      credentials_identity, forbidden_identities: dict, warnings: list,
                      budget: "_AggregateBudget | None" = None):
    """Returns (members: list[dict], baseline_snapshot_data: bytes | None)
    -- see _collect_metadata_members for the latter."""
    canonical_data_root = _validate_root(data_dir)
    if canonical_data_root is None:
        raise AgentBackupError(f"data_dir is not a valid, non-symlink directory: {data_dir}")
    canonical_base_root = _validate_root(base_dir)
    if canonical_base_root is None:
        raise AgentBackupError(f"base_dir is not a valid, non-symlink directory: {base_dir}")

    members = []
    members += _collect_database_members(data_dir, staging_root, canonical_data_root,
                                          credentials_identity, forbidden_identities, warnings,
                                          budget=budget)
    members += _collect_preference_members(data_dir, staging_root, canonical_data_root,
                                            forbidden_identities, warnings, budget=budget)
    members += _collect_custom_tool_members(data_dir, staging_root, forbidden_identities, warnings,
                                             budget=budget)
    members += _collect_audit_members(data_dir, staging_root, canonical_data_root,
                                       forbidden_identities, warnings, budget=budget)
    members += _collect_overlay_dir(base_dir, staging_root, "personas", "overlay/personas",
                                     lambda f: f.endswith(".json"), "overlay.personas",
                                     forbidden_identities, warnings, budget=budget)
    members += _collect_overlay_dir(base_dir, staging_root, os.path.join("assets", "avatars"), "overlay/avatars",
                                     lambda f: True, "overlay.avatars", forbidden_identities, warnings,
                                     budget=budget)
    members += _collect_overlay_dir(base_dir, staging_root, os.path.join("assets", "voices"), "overlay/voices",
                                     lambda f: True, "overlay.voices", forbidden_identities, warnings,
                                     budget=budget)
    members += _collect_overlay_dir(base_dir, staging_root, "skills", "overlay/skills",
                                     lambda f: f.endswith(".md"), "overlay.skills", forbidden_identities, warnings,
                                     budget=budget)
    members += _collect_overlay_dir(base_dir, staging_root, "tool_profiles", "overlay/tool_profiles",
                                     lambda f: f.endswith(".json"), "overlay.tool_profiles",
                                     forbidden_identities, warnings, budget=budget)
    members += _collect_project_members(data_dir, base_dir, staging_root, forbidden_identities, warnings,
                                         budget=budget)
    metadata_members, baseline_snapshot_data = _collect_metadata_members(
        staging_root, identity_trailers_path, baseline_hashes_path, forbidden_identities, warnings,
        budget=budget)
    members += metadata_members
    return members, baseline_snapshot_data


_REGENERATE = [
    {
        "logical_id": "state.databases.ledger_db",
        "state_class": "REBUILDABLE_GENERATED",
        "restore_policy": "always_regenerate_empty",
        "note": ("24h idempotency/dedupe cache. No archive_path, no sha256, no size -- "
                  "its content is deliberately never read, snapshotted, or hashed at all. "
                  "Restore always regenerates it empty; stale rows are never restored."),
    },
]

_EXCLUSIONS = [
    {
        "logical_id": "credentials",
        "reason": "excluded_by_design",
        "note": ("~/.config/lumina/credentials.json (or LUMINA_SECRETS_PATH) -- security "
                  "boundary. This module never reads its contents; every candidate file's "
                  "identity is checked (device+inode) against it -- for plain files, bound "
                  "to the descriptor actually opened for reading; for SQLite sources, before "
                  "and immediately after the SQLite connection is opened -- before being "
                  "trusted."),
    },
    {
        "logical_id": "scheduled_tasks",
        "reason": "known_product_limitation",
        "note": ("core/task_queue.py's scheduler is a process-local in-memory heap; the "
                  "product does not persist it today."),
    },
    {
        "logical_id": "worktrees",
        "reason": "ephemeral",
        "note": "DATA_DIR/worktrees/ -- session-local coding-agent git working copies.",
    },
    {
        "logical_id": "scratch_tmp",
        "reason": "ephemeral",
        "note": "sandbox_tmp, browser_screenshots, eval harness fixtures under BASE_DIR.",
    },
]


# ---------------------------------------------------------------------
# Destination preflight (Blocker 5)
# ---------------------------------------------------------------------

def _reject_unsafe_destination(dest_path: str, data_dir: str, base_dir: str,
                                identity_trailers_path: str | None) -> None:
    if os.path.lexists(dest_path) and os.path.islink(dest_path):
        raise AgentBackupError(f"backup destination must not be a symlink: {dest_path}")

    dest_real = os.path.realpath(dest_path)
    for label, root in (("DATA_DIR", data_dir), ("BASE_DIR", base_dir)):
        if not os.path.isdir(root):
            continue
        root_real = os.path.realpath(root)
        if dest_real == root_real or dest_real.startswith(root_real + os.sep):
            raise AgentBackupError(f"backup destination must not be inside {label}: {dest_path}")

    identity_path = identity_trailers_path or os.path.expanduser("~/.config/lumina/identity_trailers.json")
    if os.path.exists(identity_path) and dest_real == os.path.realpath(identity_path):
        raise AgentBackupError(f"backup destination must not overwrite identity_trailers.json: {dest_path}")


# ---------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------

class _BuiltArchiveResult:
    """AGENT-BACKUP-RESTORE-A2R7 (Section 10-14, BLOCKER, A2F7): one
    private, IMMUTABLE-BY-CONSTRUCTION capture of exactly what a single
    _build_agent_backup_core call actually built, verified, and
    published. Every field is a plain, already-realized Python value
    (bytes/str/int/dict) computed once, synchronously, before this
    function returns -- never a file descriptor, never anything that
    requires a LATER read of ANY filesystem path to resolve.

    A2F6 fixed AGENT-BACKUP-RESTORE-A2R6's PATH-level replacement attack
    (dest_path repointed at a different inode) by binding evidence to a
    file descriptor opened on the built archive's own inode before the
    publishing os.replace(). AGENT-BACKUP-RESTORE-A2R7 proved that fix
    was still incomplete: a live file descriptor reflects an inode's
    CURRENT bytes at the moment it is READ, not a frozen snapshot from
    the moment it was OPENED. A2F6's own design opened `.fd` early but
    deferred the actual read to whichever caller wanted receipt evidence
    (build_agent_backup_with_receipt, called sometime after
    _build_agent_backup_core had already returned) -- a same-inode
    IN-PLACE rewrite (no os.replace(), no new inode at all, just an
    ordinary open+write on the existing published path) occurring in
    that deferred window would silently hand back the REWRITTEN bytes to
    that later read, while the caller still believed it was reading the
    verified build. Section 18's own words: 'same dev/inode != same
    archive bytes' -- inode identity was never sufficient, and no amount
    of additional identity-checking machinery can make a MUTABLE
    descriptor immune to this; the only fix is to never defer the read.

    `.built_data`/`.built_sha256`/`.built_size` are therefore read and
    hashed EAGERLY, synchronously, immediately after verify_agent_backup
    succeeds and BEFORE the publishing os.replace() even runs -- at that
    moment the bytes live at a private, randomly-named staging path only
    this process knows, with no possible external interference. Once
    captured, these three fields describe archive A permanently: no
    subsequent mutation of dest_path -- whether a path-level replacement
    or a same-inode in-place rewrite -- can ever cause them to describe
    anything else, because nothing about them is ever recomputed from a
    filesystem path again.

    `.publication_observation` is Section 16's explicitly SEPARATE,
    honestly time-bounded evidence: the result of one final, as-late-as-
    practical re-read of dest_path (via the SAME universal physical-file
    capture boundary as every other source), confirmed to match
    `.built_sha256` before this object is ever constructed. A mismatch
    at that check raises AgentBackupError instead of constructing this
    object at all (Section 17: 'do not issue a successful build
    receipt') -- so a _BuiltArchiveResult existing at all already implies
    the observation succeeded; `.publication_observation` is retained
    purely so a receipt can report WHEN and WHERE that observation
    happened, never as a promise that dest_path remains that way a
    moment after this object is returned (Section 11/19: that promise is
    not achievable against an arbitrary concurrent writer, and this
    module does not claim otherwise).

    AGENT-BACKUP-RESTORE-A2F8 addendum: `.manifest` is now `report[
    "manifest"]` from the SAME strict verification that produced
    `.built_data`/`.built_sha256`/`.built_size` (via
    _capture_and_verify_archive), rather than the pre-serialization
    Python dict built before zipping -- one immutable captured object is
    now the source of every field this class carries (Section 4).
    `.built_identity` is now the identity of the fresh publication temp
    file _publish_immutable_bytes wrote `.built_data` to (whatever inode
    ended up bound to dest_path via that call's os.replace()) -- purely
    informational; publication itself is addressed by content hash, not
    by identity (Section 6/18: 'inode equality is not content
    equality')."""
    __slots__ = ("manifest", "built_data", "built_sha256", "built_size", "dest_path",
                 "built_identity", "publication_observation")

    def __init__(self, manifest, built_data, built_sha256, built_size, dest_path,
                 built_identity, publication_observation):
        self.manifest = manifest
        self.built_data = built_data
        self.built_sha256 = built_sha256
        self.built_size = built_size
        self.dest_path = dest_path
        self.built_identity = built_identity
        self.publication_observation = publication_observation


def _capture_and_verify_archive(path_to_capture: str, forbidden_identities: dict,
                                 canonical_root_real: str, label: str,
                                 resident_bytes_hint: int = 0):
    """AGENT-BACKUP-RESTORE-A2R8/A2F8 (Section 2-6, BLOCKER B1): captures
    `path_to_capture`'s exact bytes via the SAME universal stable-capture
    boundary every other physical source in this module uses (fstat/read/
    fstat bracketed double-read proof, bounded retry against a genuine
    concurrent-writer race), then strictly verifies THOSE EXACT captured
    bytes (io.BytesIO) -- never a second, separate reopen of
    path_to_capture for verification. This is the single choke point that
    makes the literal A2R8 B1 reproduction (verify path A, swap the
    pathname to verifier-valid B, reopen and read B, B silently inherits
    A's PASS) structurally impossible: there is exactly one open+read of
    `path_to_capture` in this function, and verification runs against the
    bytes that read produced, not against the pathname a second time.

    Used for BOTH a freshly-built candidate archive (tmp_path, before
    publication) and an already-published existing destination (dest_path,
    before this build is allowed to replace it -- Section 12-13, BLOCKER
    B3) -- the same evidentiary standard applies to both: an archive this
    module is about to trust, in either direction, must be captured once
    and proven valid from that exact capture.

    Returns (data, sha256, size, manifest) on a clean, valid capture and
    verification. Raises AgentBackupError otherwise -- on a capture
    failure (symlink, hardlink, forbidden identity, persistent
    instability, genuine I/O error) OR a strict-verification failure
    (structurally invalid archive, failed manifest/member/ZIP-structure
    checks) alike. The caller decides what that means for ITS OWN
    situation: a candidate archive that fails this must never be
    published at all; an existing destination that fails this must be
    left completely untouched rather than destroyed (Section 13:
    'preserving an unknown existing backup is preferable to publishing
    over it without recoverable rollback').

    `resident_bytes_hint` (AGENT-BACKUP-RESTORE-A2F11, Section 3/10/11):
    forwarded verbatim to verify_agent_backup -- bytes this call's OWN
    caller (_build_agent_backup_core) already knows are resident for
    reasons OTHER than the archive being captured/verified here (e.g.
    this build's own already-collected new-member payload, still
    resident while this same build now verifies its existing prior
    destination). Defaults to 0."""
    data, kind, reason, _attempts = _open_and_capture_stable_bytes(
        path_to_capture, canonical_root_real, forbidden_identities)
    if kind != "ok":
        raise AgentBackupError(
            f"{label} could not be captured as a single stable snapshot ({reason}): {path_to_capture}"
        )
    # Calls the module-level verify_agent_backup (never
    # _verify_agent_backup_inner directly) -- the same public,
    # never-raises, monkeypatchable seam every other caller in this
    # module goes through, now fed the captured bytes (io.BytesIO)
    # instead of a pathname.
    report = verify_agent_backup(io.BytesIO(data), resident_bytes_hint=resident_bytes_hint)
    if not report["valid"]:
        raise AgentBackupError(f"{label} failed strict verification ({path_to_capture}): {report['errors']}")
    sha256 = hashlib.sha256(data).hexdigest()
    return data, sha256, len(data), report["manifest"]


def _publish_immutable_bytes(data: bytes, expected_sha256: str, dest_path: str, dest_dir: str,
                              dest_dir_real: str, forbidden_identities: dict, label: str) -> dict:
    """AGENT-BACKUP-RESTORE-A2R8/A2F8 (Section 6/14-18, closing BLOCKERs
    B1's residual publication window and B3): writes `data` -- already-
    realized, immutable bytes whose sha256 is already known -- to a
    fresh, private, randomly-named temp file in dest_dir (NEVER a
    predictable or reusable pathname -- Section 15's explicit requirement
    that rollback authority is never a mutable, guessable recovery
    filename), fsyncs it, atomically replaces dest_path with it, best-
    effort fsyncs the containing directory, and then independently
    re-observes dest_path via the SAME universal physical-file capture
    boundary every other source in this module uses -- confirming BY
    CONTENT (never merely by inode identity; AGENT-BACKUP-RESTORE-A2R7's
    own Section 18 lesson, 'inode equality is not content equality') that
    dest_path now holds exactly `data`.

    Used identically for forward publication of a freshly built archive
    and for rollback republication of an immutable prior snapshot -- one
    evidence-producing mechanism ('write immutable bytes, replace
    atomically, verify the result by content'), not two independently-
    written, potentially-divergent implementations of the same idea.

    Returns {"destination", "observed_sha256", "matched_built_artifact",
    "published_identity", "directory_fsynced", "checked_at"} on success
    (AGENT-BACKUP-RESTORE-A2F10, HIGH H1: `directory_fsynced` is a
    separate, honestly-reported fact -- best-effort parent-directory
    durability, distinct from content-publication success, which this
    function never fails BECAUSE of a directory-fsync failure alone).
    Raises
    AgentBackupError if the post-publish re-observation cannot be made at
    all, or observes bytes other than `data` -- e.g. a same-inode
    in-place rewrite, or a further path-level replacement, racing this
    very publish (never returns a partial or unconfirmed result). A raw
    OSError from writing/fsyncing/replacing the temp file itself
    propagates unwrapped -- an infrastructure failure (disk full,
    permission denied), not a security-relevant mismatch, so it is never
    disguised as an AgentBackupError."""
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=".agent_backup_pub_", suffix=".tmp", dir=dest_dir)
    published_identity = None
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            fst = os.fstat(f.fileno())
            published_identity = (fst.st_dev, fst.st_ino)
        os.replace(tmp_path, dest_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    # AGENT-BACKUP-RESTORE-A2F10 (HIGH H1, Section 13-17): directory
    # durability is EVIDENCE, not something silently assumed -- the
    # underlying fsync attempt was already only ever best-effort (not
    # every filesystem/platform supports directory fsync), but its
    # outcome used to be swallowed entirely rather than reported. Content
    # publication success and durability confirmation are two distinct
    # facts from here on: this function still returns normally either
    # way (a directory-fsync failure never turns an otherwise-successful,
    # content-verified publish into a raised error -- Section 14: "do not
    # turn A1's historical best-effort durability into an artificial
    # 'fsync failed therefore corrupt' rule"), but the caller can now see
    # which of the two actually happened.
    directory_fsynced = _best_effort_fsync_dir(dest_dir)

    observed_data, kind, reason, _attempts = _open_and_capture_stable_bytes(
        dest_path, dest_dir_real, forbidden_identities)
    if kind != "ok":
        raise AgentBackupError(
            f"could not re-observe {label} immediately after publishing it to {dest_path} ({reason})"
        )
    observed_sha256 = hashlib.sha256(observed_data).hexdigest()
    if observed_sha256 != expected_sha256:
        raise AgentBackupError(
            f"observed different bytes than {label} that was just published to {dest_path} "
            f"(expected {expected_sha256}, observed {observed_sha256}) -- same-inode content "
            f"mutation or a further replacement racing the publish itself"
        )
    return {
        "destination": dest_path,
        "observed_sha256": observed_sha256,
        "matched_built_artifact": True,
        "published_identity": published_identity,
        "directory_fsynced": directory_fsynced,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def _best_effort_fsync_dir(dir_path: str) -> bool:
    """fsyncs `dir_path` itself (durability for a just-renamed directory
    entry, not merely the file's own content) and reports whether it
    actually succeeded -- best-effort because not every filesystem/
    platform supports directory fsync (AGENT-BACKUP-RESTORE-A2R9, Section
    15: 'if directory fsync is unsupported on a platform, report the
    durability limitation truthfully; do not silently claim durable
    recovery'). Returns True only on confirmed success."""
    try:
        dirfd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(dirfd)
        return True
    except OSError:
        return False
    finally:
        os.close(dirfd)


def _unverified_recovery_result(path, expected_sha256: str, observed_sha256, reason: str,
                                 directory_fsynced: bool) -> dict:
    """Shared failure-shape constructor for _preserve_rollback_failure_
    artifact -- AGENT-BACKUP-RESTORE-A2R9 (Section 16): never call an
    unverified or partially-preserved artifact 'recovery preserved';
    every non-success path returns this same honestly-labeled shape."""
    return {
        "path": path,
        "expected_sha256": expected_sha256,
        "observed_sha256": observed_sha256,
        "verified_at_preservation": False,
        "directory_fsynced": directory_fsynced,
        "note": (f"UNVERIFIED recovery artifact at {path!r} -- {reason}" if path
                 else f"recovery artifact could not be preserved at all -- {reason}"),
    }


def _preserve_rollback_failure_artifact(data: bytes, dest_dir: str, dest_dir_real: str,
                                         forbidden_identities: dict, expected_sha256: str) -> dict:
    """AGENT-BACKUP-RESTORE-A2R9 (Section 12-16, BLOCKER): the absolute
    last resort, reached only when BOTH the new build's own publication
    AND the immutable-prior-bytes rollback have failed. A2R8/A2F8's own
    version of this function wrote `data` to a randomly-named file,
    fsynced it, and returned the bare path -- WITHOUT ever proving the
    resulting path still contains `data` (A2R9's own finding: 'recovery
    preservation... does [this] without proving the resulting path still
    contains expected A'). That is incomplete: an artifact reported as
    'preserved' with no content verification is not actually known-good.

    v1's contract (Section 13): write to a fresh, exclusive, randomly-
    named temp file; fsync the file; atomically finalize it (os.replace
    to a final name, so no reader can ever observe a partially-written
    recovery artifact under its final name); best-effort fsync the
    containing directory; RE-OBSERVE the finalized artifact via the SAME
    universal stable-capture boundary every other source in this module
    uses (catching a same-inode overwrite/truncate/append race during or
    immediately after finalization, not merely trusting the write
    succeeded); require SHA256(observed) == expected_sha256; strictly
    re-verify the observed bytes as a valid Agent Backup archive
    (verify_agent_backup) -- only if ALL of that holds is
    verified_at_preservation ever True. Any failure along the way still
    reports whatever path/bytes did make it to disk (Section 16: partial/
    unverified artifacts are removed unless they are the only potentially
    recoverable copy -- and by construction, reaching this function AT
    ALL means both the forward publish and the rollback have already
    failed, so whatever this function manages to preserve, however
    unverified, IS that last remaining copy; it is therefore always kept,
    never deleted, but NEVER described as verified unless every check
    above actually passed) -- labeled UNVERIFIED, never 'recovery
    preserved' / 'known-good', with the specific reason recorded.

    Never raises. Returns a dict: {"path", "expected_sha256",
    "observed_sha256", "verified_at_preservation", "directory_fsynced",
    "note"}. `path` is None only if not even the initial write reached
    disk at all."""
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".agent_backup_ROLLBACK_FAILED_RECOVERY_", suffix=".zip.part", dir=dest_dir)
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            return _unverified_recovery_result(
                None, expected_sha256, None,
                f"could not write recovery artifact: {e}", directory_fsynced=False)

        final_path = tmp_path[: -len(".part")]
        try:
            os.replace(tmp_path, final_path)
        except OSError as e:
            return _unverified_recovery_result(
                tmp_path, expected_sha256, None,
                f"wrote recovery bytes but could not atomically finalize the artifact ({e}) "
                f"-- raw bytes may still be present at the unfinalized temp path",
                directory_fsynced=False)
        tmp_path = None  # ownership transferred to final_path; nothing left to clean up here

        directory_fsynced = _best_effort_fsync_dir(dest_dir)

        observed_data, kind, reason, _attempts = _open_and_capture_stable_bytes(
            final_path, dest_dir_real, forbidden_identities)
        if kind != "ok":
            return _unverified_recovery_result(
                final_path, expected_sha256, None,
                f"could not re-observe the recovery artifact immediately after finalizing "
                f"it ({reason})", directory_fsynced=directory_fsynced)

        observed_sha256 = hashlib.sha256(observed_data).hexdigest()
        if observed_sha256 != expected_sha256:
            return _unverified_recovery_result(
                final_path, expected_sha256, observed_sha256,
                "the recovery artifact's observed content does not match the immutable "
                "prior bytes it was supposed to preserve (a same-inode mutation or "
                "replacement raced its own finalization)",
                directory_fsynced=directory_fsynced)

        verify_report = verify_agent_backup(io.BytesIO(observed_data))
        if not verify_report["valid"]:
            return _unverified_recovery_result(
                final_path, expected_sha256, observed_sha256,
                f"the recovery artifact's content-verified bytes do not strictly verify as "
                f"a valid Agent Backup archive: {verify_report['errors']}",
                directory_fsynced=directory_fsynced)

        return {
            "path": final_path,
            "expected_sha256": expected_sha256,
            "observed_sha256": observed_sha256,
            "verified_at_preservation": True,
            "directory_fsynced": directory_fsynced,
            "note": (
                "recovery artifact content-verified (exact-content recapture, hash "
                "comparison, and strict Agent Backup re-verification) immediately after "
                "preservation -- this describes the artifact AT THAT MOMENT, not a promise "
                "the mutable recovery path can never change again afterward"
            ),
        }
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _build_agent_backup_core(data_dir: str, base_dir: str, dest_path: str,
                              identity_trailers_path: str | None = None,
                              baseline_hashes_path: str | None = None,
                              credentials_path: str | None = None) -> "_BuiltArchiveResult":
    """Collects a full Agent Backup archive at dest_path and returns a
    _BuiltArchiveResult binding the manifest to the exact archive bytes
    this call published (Section 15-18, A2F6; single-snapshot capture and
    verification, Section 2-6, A2F8) -- the shared implementation behind
    both public entry points, build_agent_backup (which only wants
    .manifest) and build_agent_backup_with_receipt (which additionally
    needs .built_data/.built_sha256 to construct honest build-time
    evidence without ever reopening dest_path by name after the fact).

    data_dir / base_dir are required, explicit parameters, never read from
    the `config` module directly -- safely testable against a tmp_path
    fixture with zero risk of touching a real installation.

    baseline_hashes_path defaults to <base_dir>/metadata/shipped_baseline_hashes.json
    if not given. If the artifact is missing, wrong-format, or bound to a
    different lumina_version than this build, every USER_OVERLAY member's
    provenance truthfully falls back to 'unknown' rather than a fabricated
    classification (see _load_baseline_hashes).

    Destination is preflight-checked before any collection work begins.
    Sources are staged into an isolated temp directory (SQLite stores via
    the online backup API, everything else via a boundary-checked,
    TOCTOU-closed, stable-capture-proven byte copy) before anything is
    hashed or archived -- archive construction reads staged bytes only,
    never the live source again. The finished archive's bytes are
    captured once and strictly verified from that exact capture (Section
    2-6, A2F8 -- _capture_and_verify_archive); only a clean verify
    triggers publication. If dest_path already exists, its own bytes are
    likewise captured once and strictly verified BEFORE publication is
    even attempted (Section 12-13, A2F8) -- an existing destination that
    cannot be faithfully captured or verified blocks the whole build,
    leaving it untouched, rather than being silently overwritten.
    Publication itself (_publish_immutable_bytes) writes the immutable
    built bytes to a fresh, private temp file, atomically replaces
    dest_path, and independently re-observes the result BY CONTENT (never
    merely by inode identity -- Section 18) before this call ever hands
    back evidence that could be mistaken for describing anything else. A
    detected mismatch attempts to restore the immutable prior snapshot
    (Section 14-18, A2F8) rather than leaving dest_path in a known-bad,
    unattested state.
    """
    _reject_unsafe_destination(dest_path, data_dir, base_dir, identity_trailers_path)

    if baseline_hashes_path is None:
        baseline_hashes_path = _default_baseline_hashes_path(base_dir)

    credentials_identity = _resolve_credentials_identity(credentials_path)
    ledger_identity = _resolve_ledger_identity(data_dir)
    forbidden_identities = _build_forbidden_identities(credentials_identity, ledger_identity)
    lumina_version = _read_lumina_version(base_dir)

    # AGENT-BACKUP-RESTORE-A2F10 (HIGH H2, Section 20-27): a cheap stat
    # (never a read) of whatever archive already occupies dest_path --
    # rollback authority may hold that whole prior archive resident
    # (Section 24) while a fresh build is collected, so it must shrink
    # the resource budget available to THIS build's own new members,
    # never be ignored.
    prior_backup_bytes = 0
    if os.path.lexists(dest_path):
        try:
            prior_backup_bytes = os.stat(dest_path).st_size
        except OSError:
            prior_backup_bytes = 0
    budget = _AggregateBudget(_determine_aggregate_payload_budget(prior_backup_bytes))

    staging_root = tempfile.mkdtemp(prefix=".agent_backup_staging_")
    try:
        warnings = []
        members, baseline_snapshot_data = _collect_members(
            data_dir, base_dir, staging_root, identity_trailers_path, baseline_hashes_path,
            credentials_identity, forbidden_identities, warnings, budget=budget)

        # AGENT-BACKUP-RESTORE-A2F10 (Section 8-10, part of B1): fail
        # BEFORE ever attempting to build the ZIP if the physical entry
        # count (every collected member, plus the one always-generated
        # manifest.json) would reach the ambiguous ZIP64-sentinel zone --
        # see _MAX_PHYSICAL_ZIP_ENTRIES's own module-level comment for
        # the exact empirical boundary this eliminates.
        total_physical_entries = len(members) + 1
        if total_physical_entries > _MAX_PHYSICAL_ZIP_ENTRIES:
            raise AgentBackupError(
                f"this backup would produce {total_physical_entries} physical ZIP entries "
                f"({len(members)} collected member(s) plus manifest.json), exceeding Agent "
                f"Backup v1's maximum of {_MAX_PHYSICAL_ZIP_ENTRIES} -- Lumina's own strict "
                f"verifier cannot distinguish a genuine {_MAX_PHYSICAL_ZIP_ENTRIES + 1}-entry "
                f"archive from a ZIP64 sentinel value and must reject it either way; refusing "
                f"to build an archive this same build would immediately refuse to trust"
            )

        archive_paths_present = {m["archive_path"] for m in members}
        for required_path, label in (
            ("state/databases/lumina.db", "primary database"),
            ("state/preferences/prefs.json", "preferences"),
            ("state/telemetry/flight_recorder.db", "Flight Recorder database"),
        ):
            if required_path not in archive_paths_present:
                raise AgentBackupError(
                    f"collection completed but {required_path} ({label}) is not among "
                    f"the staged members -- refusing to produce an incomplete backup"
                )

        # AGENT-BACKUP-RESTORE-A2R8/A2F8 (BLOCKER B2): parse the SAME
        # captured bytes _collect_metadata_members already staged into the
        # archive -- baseline_hashes_path itself is never reopened by name
        # a second time here. baseline_snapshot_data is None whenever the
        # file is genuinely absent/unconfigured, exactly the case
        # _parse_baseline_hashes's caller (formerly _load_baseline_hashes
        # itself) already treated as "no baseline -- provenance unknown".
        if baseline_snapshot_data is not None:
            baseline, baseline_warning = _parse_baseline_hashes(
                baseline_snapshot_data, lumina_version, base_dir, source_label=baseline_hashes_path)
        else:
            baseline, baseline_warning = {}, None
        if not baseline:
            warnings.append(baseline_warning or (
                "no baseline_hashes_path supplied (or file unreadable) -- every "
                "USER_OVERLAY member's provenance defaults to 'unknown' rather "
                "than shipped_baseline_unmodified/modified"
            ))

        public_members = []
        for m in members:
            # AGENT-BACKUP-RESTORE-A2R9 (Section 2-8, BLOCKER B1/B2): hash
            # and size are derived from m["data"] -- the immutable bytes
            # captured once, at collection time -- never from a fresh
            # os.path.getsize()/_sha256_file() reopen of m["staged_path"]
            # by name. A staging pathname is transport/cache only from
            # here on; nothing downstream ever rereads it (Section 11).
            # `data` being anything other than real bytes at this point
            # would be an internal contract violation of every collector
            # above, not an external race to defend against -- structurally
            # impossible by construction now, unlike the old staged-file
            # existence check this replaces.
            data = m["data"]
            sha256 = hashlib.sha256(data).hexdigest()
            size = len(data)
            public = {k: v for k, v in m.items() if k not in ("staged_path", "data", "_baseline_key")}
            public["sha256"] = sha256
            public["size"] = size
            if "_baseline_key" in m:
                public["provenance"] = _classify_provenance(m["_baseline_key"], sha256, baseline)
            public_members.append(public)

        manifest = {
            "format": MANIFEST_FORMAT,
            "format_version": MANIFEST_FORMAT_VERSION,
            "lumina_version": lumina_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_platform": {"os": platform.system().lower(), "python": platform.python_version()},
            "source_data_root": {
                "note": "informational only -- never trusted literally by Restore",
                "data_dir": data_dir,
                "base_dir": base_dir,
            },
            "backup_mode": "full",
            "compatibility": {
                "min_supported_format_version": {"major": 1, "minor": 0},
                "max_known_format_version": MANIFEST_FORMAT_VERSION,
                "unknown_newer_major_policy": "refuse",
                "unknown_newer_minor_policy": "accept_ignore_unknown_optional_fields",
                "older_same_major_policy": "supported_with_migration_if_minor_delta",
                "older_major_mismatch_policy": "refuse_direct_to_explicit_migration_tool",
            },
            "hash_algorithm": "sha256",
            "members": public_members,
            "regenerate": _REGENERATE,
            "exclusions": _EXCLUSIONS,
            "warnings": warnings,
        }

        _validate_namespace(public_members)

        dest_dir = os.path.dirname(os.path.abspath(dest_path)) or "."
        os.makedirs(dest_dir, exist_ok=True)
        dest_dir_real = os.path.realpath(dest_dir)

        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".agent_backup_", suffix=".zip.tmp", dir=dest_dir)
        os.close(tmp_fd)
        try:
            try:
                # AGENT-BACKUP-RESTORE-A2R9 (Section 26-29, HIGH/RELEASE
                # GATE): allowZip64 defaulted to True (zipfile's own
                # default), so the builder could silently emit a ZIP64
                # archive (e.g. >65535 members) that this module's own
                # strict verifier -- which never accepts ZIP64 (Section
                # 20/27, unchanged) -- would then reject. Builder and
                # verifier must agree on one v1 ZIP64 policy: v1 is
                # intentionally non-ZIP64, enforced HERE at build time
                # (never silently create an archive the same build would
                # later refuse) rather than only at verify time.
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
                    for m in members:
                        # AGENT-BACKUP-RESTORE-A2R9 (Section 2-8, BLOCKER
                        # B1/B2/B3): every physical member's authoritative
                        # content is now `m["data"]` -- immutable bytes
                        # captured once, at collection time (see
                        # _stage_physical_state_file/_snapshot_sqlite) --
                        # never a staging pathname reopened here. zf.write()
                        # would reopen and reread m["staged_path"] by name a
                        # SECOND time (a third time, counting the read that
                        # produced m["data"] itself), reintroducing exactly
                        # the mutable-staging-authority gap this pass
                        # closes; zf.writestr() never touches the filesystem
                        # for the member's content at all.
                        zf.writestr(_make_v1_zipinfo(m["archive_path"]), m["data"])
                    # AGENT-BACKUP-RESTORE-A2F11 (P0, Section 21-27):
                    # manifest.json now goes through the SAME canonical
                    # ZipInfo construction as every payload member above,
                    # rather than a bare-string zf.writestr("manifest.json",
                    # ...) call -- source-vetted to leave
                    # zipfile.ZipFile.writestr's OWN default external_attr
                    # (0o600 << 16, permission `rw-------`) in effect, a
                    # real, previously undocumented discrepancy from every
                    # other member's explicit 0o644 << 16 (`rw-r--r--`).
                    # One canonical member metadata contract means ALL
                    # physical members share it, manifest.json included.
                    zf.writestr(_make_v1_zipinfo("manifest.json"), json.dumps(manifest, indent=2))
            except zipfile.LargeZipFile as e:
                raise AgentBackupError(
                    f"this backup would require ZIP64 extensions ({e}) -- Agent Backup v1 "
                    f"is intentionally non-ZIP64 (its strict verifier never accepts a ZIP64 "
                    f"archive, so the builder must never silently create one either); refusing "
                    f"to publish an archive this same build would later reject"
                )
            # fsync AFTER the ZipFile context manager has fully closed the
            # file and written its central directory -- fsyncing mid-write
            # (the A2R-reproduced ordering bug) can sync a file that isn't
            # actually a complete, valid zip yet.
            fd = os.open(tmp_path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

            # AGENT-BACKUP-RESTORE-A2R8/A2F8 (Section 2-6, BLOCKER B1):
            # capture tmp_path's exact bytes ONCE and strictly verify
            # THOSE bytes -- there is no verify(tmp_path) followed by a
            # later, separate reopen of tmp_path anywhere in this call
            # chain any more. built_manifest (not the pre-serialization
            # `manifest` local built above) becomes this result's
            # .manifest, so every historical field this function returns
            # is derived from the one immutable captured object.
            #
            # AGENT-BACKUP-RESTORE-A2F11 (Section 3/10/11): `members`'
            # own captured `data` bytes are STILL resident right now
            # (the list stays in scope for the rest of this function,
            # e.g. archive_paths_present below) -- self-verification's
            # own host-safe decompression budget must be derived knowing
            # that, not computed as if this were the only thing resident.
            already_collected_payload_bytes = sum(len(m["data"]) for m in members)
            built_data, built_sha256, built_size, built_manifest = _capture_and_verify_archive(
                tmp_path, forbidden_identities, dest_dir_real, "freshly-built archive",
                resident_bytes_hint=already_collected_payload_bytes)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        # AGENT-BACKUP-RESTORE-A2R8/A2F8 (Section 12-13, BLOCKER B3):
        # capture and strictly verify whatever ALREADY exists at
        # dest_path -- ONCE -- before this build is allowed to touch it
        # at all. An existing destination that cannot be faithfully
        # captured or does not strictly verify is preserved, never
        # destroyed: this raises (via _capture_and_verify_archive) and
        # the whole build fails BEFORE any publication step below has
        # run, rather than overwriting an unknown/unverifiable existing
        # backup with no safe way back (Section 13).
        #
        # AGENT-BACKUP-RESTORE-A2F11 (Section 3/10/11): by this point
        # BOTH the collected members' own bytes AND built_data (the
        # just-built, not-yet-published candidate archive) are
        # simultaneously resident -- the prior-destination verification
        # below is charged for both, not just its own compressed bytes.
        prior_snapshot = None
        if os.path.lexists(dest_path):
            prior_data, prior_sha256, _prior_size, _prior_manifest = _capture_and_verify_archive(
                dest_path, forbidden_identities, dest_dir_real, "existing destination archive",
                resident_bytes_hint=already_collected_payload_bytes + len(built_data))
            prior_snapshot = (prior_data, prior_sha256)

        # Section 6: publish FROM the immutable captured bytes via a
        # fresh, private, single-use temp file -- tmp_path (already
        # deleted above) is never involved in publication at all, closing
        # even the residual window between "capture built_data" and "the
        # os.replace() that publishes it" that would otherwise still
        # exist if tmp_path itself were renamed into place.
        try:
            publication_observation = _publish_immutable_bytes(
                built_data, built_sha256, dest_path, dest_dir, dest_dir_real,
                forbidden_identities, "freshly-built archive")
        except AgentBackupError as publish_error:
            # Section 14-18 (BLOCKER B3): the destination is now
            # known-tampered -- attempt to restore the IMMUTABLE prior
            # snapshot (never a mutable recovery pathname) via the exact
            # same write-fresh-temp/fsync/replace/re-observe mechanism a
            # normal publish uses, so a successful rollback is held to
            # the identical evidentiary standard. If no prior destination
            # existed, or the rollback itself fails, that is reported
            # honestly too, never silently.
            if prior_snapshot is None:
                raise AgentBackupError(
                    f"destination changed before build receipt could be finalized "
                    f"({publish_error}): {dest_path} -- no prior destination existed to restore"
                )
            prior_data, prior_sha256 = prior_snapshot
            try:
                # AGENT-BACKUP-RESTORE-A2F10 (HIGH H1, Section 17,
                # "Rollback Durability Truth"): content restoration and
                # directory-durability confirmation are separate facts
                # here too -- never claim "durably restored" when the
                # parent-directory fsync did not actually succeed, and
                # never call the rollback itself "failed" when content
                # restoration (independently content-verified by
                # _publish_immutable_bytes's own re-observation) actually
                # succeeded.
                rollback_publication = _publish_immutable_bytes(
                    prior_data, prior_sha256, dest_path, dest_dir, dest_dir_real,
                    forbidden_identities, "prior destination (rollback)")
                rollback_note = (
                    "prior destination restored (content independently re-observed and "
                    "confirmed by hash; directory durability "
                    + ("confirmed)" if rollback_publication.get("directory_fsynced")
                       else "best-effort only, not confirmed)")
                )
            except (AgentBackupError, OSError) as rollback_error:
                # Section 16: unlike the FORWARD publish above (where a
                # raw OSError from write/fsync/replace means the atomic
                # replace never actually landed, so dest_path is
                # unchanged and needs no rollback at all), a raw OSError
                # HERE -- during the rollback attempt itself -- still
                # leaves dest_path in the bad state the forward publish's
                # own failure put it in: the good prior bytes were never
                # restored either way. Both exception types are therefore
                # "rollback failed" for this specific attempt, and both
                # must still try to preserve a last-resort recovery
                # artifact rather than silently losing the prior bytes.
                #
                # Do not delete this recovery artifact in any generic
                # generic cleanup -- it is a local variable here, never
                # wired into a cleanup path at all, and its filename is
                # never treated as authoritative by any later automated
                # decision in this module; only its bytes/hash are.
                #
                # AGENT-BACKUP-RESTORE-A2R9 (Section 12-16, BLOCKER):
                # _preserve_rollback_failure_artifact now content-verifies
                # and strictly re-verifies the artifact before ever
                # reporting it -- the note below reflects
                # verified_at_preservation truthfully rather than always
                # saying "preserved" for whatever path happened to exist
                # on disk (Section 14: never "path containing B + expected
                # hash for A + 'recovery preserved'").
                recovery = _preserve_rollback_failure_artifact(
                    prior_data, dest_dir, dest_dir_real, forbidden_identities, prior_sha256)
                if recovery["path"] is None:
                    rollback_note = (
                        f"prior destination could NOT be restored ({rollback_error}), AND the "
                        f"last-resort recovery copy of its bytes could also not be written -- "
                        f"only this exception's own expected sha256 ({prior_sha256}) survives; "
                        f"no recovery bytes remain on disk"
                    )
                elif recovery["verified_at_preservation"]:
                    rollback_note = (
                        f"prior destination could NOT be restored ({rollback_error}) -- its "
                        f"bytes are VERIFIED-preserved at {recovery['path']!r} (content-"
                        f"recaptured, hash-confirmed, and strictly re-verified as a valid "
                        f"Agent Backup archive immediately after preservation; sha256 "
                        f"{recovery['observed_sha256']}, directory durability "
                        f"{'confirmed' if recovery['directory_fsynced'] else 'best-effort only, not confirmed'}) "
                        f"for manual recovery"
                    )
                else:
                    rollback_note = (
                        f"prior destination could NOT be restored ({rollback_error}) -- an "
                        f"UNVERIFIED recovery artifact was preserved at {recovery['path']!r} "
                        f"({recovery['note']}); expected sha256 {prior_sha256}. This artifact "
                        f"is NOT confirmed to hold the correct bytes -- manual inspection is "
                        f"required before trusting it"
                    )
            raise AgentBackupError(
                f"destination changed before build receipt could be finalized "
                f"({publish_error}): {dest_path} -- {rollback_note}"
            )

        return _BuiltArchiveResult(built_manifest, built_data, built_sha256, built_size, dest_path,
                                    publication_observation.get("published_identity"), publication_observation)
    finally:
        _rmtree_best_effort(staging_root)


def build_agent_backup(data_dir: str, base_dir: str, dest_path: str, **kwargs) -> dict:
    """Public entry point: collect a full Agent Backup archive at
    dest_path and return the EXACT manifest dict archived as
    manifest.json -- byte-for-byte what a strict re-verification of the
    published archive would itself parse back out, never a
    post-publication-mutated copy. A thin wrapper over
    _build_agent_backup_core, which does the actual work and
    additionally captures build-time evidence (built_data/built_sha256/
    built_identity/publication_observation) this function has no use for
    -- only build_agent_backup_with_receipt (Section 13-18) reads it.

    AGENT-BACKUP-RESTORE-A2F11 (P1, Section 32-37, repairing the A2R11
    P1 manifest-truth blocker): A2F10's own version of this function
    appended a directory-fsync-failure warning onto a shallow COPY of
    the manifest before returning it -- meaning `build_agent_backup(...)
    != json.loads(open(dest_path).read())["manifest.json"]` was possible
    whenever that specific publish's own parent-directory fsync failed,
    directly contradicting this function's own documented "returns the
    exact manifest dict written" contract (a real, source-vetted
    inconsistency, not a hypothetical one). The archive's manifest.json
    cannot honestly contain a fact (whether ITS OWN publication's
    directory-fsync succeeded) that was not yet knowable at the moment
    its bytes were serialized and archived -- so post-publication
    durability evidence no longer touches the returned manifest at all.
    `result.manifest` is returned completely unmodified, always. A
    directory-fsync failure is instead surfaced through
    AgentBackupDurabilityWarning (Section 34: 'report durability through
    an existing side channel... do not stuff post-publication evidence
    into manifest["warnings"]') -- callers who need STRUCTURED, in-band
    durability evidence (rather than a catchable warning) already have
    build_agent_backup_with_receipt's dedicated
    receipt["publication"]["directory_fsynced"] field for exactly that
    (Section 35: 'the receipt API remains the rich evidence surface');
    this function's own return shape is a pre-existing, narrow public
    contract (a manifest dict) this pass does not expand."""
    result = _build_agent_backup_core(data_dir, base_dir, dest_path, **kwargs)
    if not result.publication_observation.get("directory_fsynced", True):
        warnings.warn(
            "Agent Backup published successfully and the published content was "
            "independently re-observed and hash-confirmed, but the destination "
            "directory's own fsync did not succeed (best-effort only) -- durable "
            "survival of the directory-entry rename across an immediate host crash "
            "is not confirmed for this specific publish. The returned manifest dict "
            "is unaffected -- it is exactly what was archived as manifest.json -- "
            "this warning is the only place this fact is surfaced by this plain "
            "build_agent_backup() entry point; build_agent_backup_with_receipt's own "
            "receipt['publication']['directory_fsynced'] field carries it in "
            "structured form instead.",
            AgentBackupDurabilityWarning,
            stacklevel=2,
        )
    return result.manifest


def _rmtree_best_effort(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _validate_namespace(public_members: list[dict]) -> None:
    """Fail fast, before ever writing a zip, on a namespace that
    verify_agent_backup would reject anyway -- duplicate/collision on
    archive_path or logical_id, an unsafe path shape, or a physical
    payload outside the allowed v1 namespace."""
    errors = _check_path_and_identity_rules(public_members)
    if errors:
        raise AgentBackupError(f"member namespace is invalid, refusing to build: {errors}")


def _check_path_and_identity_rules(members: list[dict]) -> list[str]:
    """Path-shape safety, duplicate/Unicode-collision detection, and the
    forbidden-namespace allowlist (Section 8/9) -- shared between the
    build-time fail-fast check and the strict verifier, so both enforce
    exactly the same rules."""
    errors = []
    seen_paths = set()
    seen_nfc = {}
    seen_nfc_casefold = {}
    seen_logical_ids = set()
    reserved_nfc_casefold = unicodedata.normalize("NFC", "manifest.json").casefold()

    for m in members:
        archive_path = m.get("archive_path")
        logical_id = m.get("logical_id")

        if not isinstance(archive_path, str) or not archive_path:
            errors.append(f"member {logical_id!r} has an invalid archive_path: {archive_path!r}")
            continue
        if archive_path.startswith("/") or ":" in archive_path or "\\" in archive_path:
            errors.append(f"archive_path is not a portable relative path: {archive_path!r}")
        segments = archive_path.split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            errors.append(f"archive_path has an empty/'.'/'..' segment: {archive_path!r}")

        # Section 11 -- Windows portable-namespace ambiguities: a trailing
        # dot/space is stripped by Windows filesystem APIs (silently, not
        # rejected), and a segment whose stem collides with a reserved
        # device name opens an OS device rather than a file regardless of
        # extension. Neither round-trips safely, so both are rejected
        # outright here rather than silently accepted and left to fail
        # unpredictably at restore time on a Windows destination.
        for seg in segments:
            if seg in ("", ".", ".."):
                continue  # already reported above
            if seg != seg.rstrip(" ."):
                errors.append(
                    f"archive_path segment {seg!r} ends in a trailing dot/space -- "
                    f"not portable to Windows filesystems: {archive_path!r}"
                )
            stem = seg.split(".", 1)[0]
            if stem.upper() in _WINDOWS_RESERVED_NAMES:
                errors.append(
                    f"archive_path segment {seg!r} collides with a reserved Windows "
                    f"device name -- not portable: {archive_path!r}"
                )

        if archive_path in seen_paths:
            errors.append(f"duplicate archive_path: {archive_path!r}")
        seen_paths.add(archive_path)

        nfc = unicodedata.normalize("NFC", archive_path)
        if nfc in seen_nfc and seen_nfc[nfc] != archive_path:
            errors.append(
                f"Unicode NFC-normalization collision between {seen_nfc[nfc]!r} "
                f"and {archive_path!r}"
            )
        seen_nfc.setdefault(nfc, archive_path)

        folded = nfc.casefold()
        if folded in seen_nfc_casefold and seen_nfc_casefold[folded] != archive_path:
            errors.append(
                f"case-fold collision (after NFC normalization) between "
                f"{seen_nfc_casefold[folded]!r} and {archive_path!r} -- not a safe "
                f"portable namespace across case-insensitive filesystems"
            )
        seen_nfc_casefold.setdefault(folded, archive_path)

        if folded == reserved_nfc_casefold:
            errors.append(f"archive member name collides with the reserved manifest.json name: {archive_path!r}")

        if not isinstance(logical_id, str) or not logical_id:
            errors.append(f"member with archive_path {archive_path!r} has an invalid logical_id")
        elif logical_id in seen_logical_ids:
            errors.append(f"duplicate logical_id: {logical_id!r}")
        else:
            seen_logical_ids.add(logical_id)

        if not any(archive_path.startswith(prefix) for prefix in _ALLOWED_ARCHIVE_PREFIXES):
            errors.append(f"archive member outside the allowed v1 namespace: {archive_path!r}")

        basename = segments[-1] if segments else archive_path
        if "ledger" in basename.casefold():
            errors.append(f"physical ledger payload is never permitted, anywhere: {archive_path!r}")
        if any(seg.casefold() in _FORBIDDEN_NAME_SEGMENTS for seg in segments):
            errors.append(f"forbidden namespace segment in archive member: {archive_path!r}")
        if basename.casefold() in _FORBIDDEN_BASENAMES or "credential" in archive_path.lower() \
                or (isinstance(logical_id, str) and "credential" in logical_id.lower()):
            errors.append(f"member name suspiciously references credentials: {archive_path!r}")

    return errors


# ---------------------------------------------------------------------
# Verify (Blocker 6/7) -- strict backup-format self-verification against
# Lumina's own canonical v1 semantics, not just archive shape.
# ---------------------------------------------------------------------

def _validate_format_version(version) -> list[str]:
    if not isinstance(version, dict):
        return [f"format_version is malformed (must be {{'major': int, 'minor': int}}): {version!r}"]

    errors = []
    major = version.get("major")
    minor = version.get("minor")

    if isinstance(major, bool) or not isinstance(major, int):
        errors.append(f"format_version.major must be an int, not {type(major).__name__}: {major!r}")
        major = None
    if isinstance(minor, bool) or not isinstance(minor, int):
        errors.append(f"format_version.minor must be an int, not {type(minor).__name__}: {minor!r}")
        minor = None

    if major is not None:
        if major < 1:
            errors.append(f"format_version.major must be >= 1: {major!r}")
        elif major != MANIFEST_FORMAT_VERSION["major"]:
            errors.append(
                f"manifest format_version major {major} is not supported by this code "
                f"(supports major {MANIFEST_FORMAT_VERSION['major']}) -- refusing"
            )
    if minor is not None and minor < 0:
        errors.append(f"format_version.minor must be >= 0: {minor!r}")

    return errors


def _validate_canonical_policy_records(manifest: dict) -> list[str]:
    """The manifest is incomplete without its canonical policy records --
    not merely structurally valid (Section 7), and exactly one of each is
    permitted, not merely 'at least one' (Section 13:
    AGENT-BACKUP-RESTORE-A2R3 found contradictory/duplicate credentials
    exclusions and ledger regenerate entries could coexist with a valid
    canonical one and still verify). Value-checking the ledger regenerate
    entry's own state_class/restore_policy also closes A2R3's
    ledger.state_class=EXCLUDED / ledger.restore_policy=restore_stale_rows
    attacks -- presence of a record with the right logical_id was
    previously sufficient regardless of what else it claimed."""
    errors = []
    exclusions = manifest.get("exclusions")
    if isinstance(exclusions, list):
        cred_entries = [e for e in exclusions if isinstance(e, dict)
                         and e.get("logical_id") == _CANONICAL_CREDENTIALS_EXCLUSION["logical_id"]]
        if len(cred_entries) > 1:
            errors.append(
                f"manifest carries {len(cred_entries)} exclusions[] records for "
                f"logical_id=='credentials' -- exactly one canonical record is "
                f"permitted, duplicates/contradictions are never valid"
            )
        elif len(cred_entries) == 0 or cred_entries[0].get("reason") != _CANONICAL_CREDENTIALS_EXCLUSION["reason"]:
            errors.append(
                "manifest is missing the canonical credentials exclusion record "
                "(exclusions[] with logical_id=='credentials', reason=='excluded_by_design')"
            )
    regenerate = manifest.get("regenerate")
    if isinstance(regenerate, list):
        ledger_entries = [r for r in regenerate if isinstance(r, dict)
                           and r.get("logical_id") == _CANONICAL_LEDGER_LOGICAL_ID]
        if len(ledger_entries) > 1:
            errors.append(
                f"manifest carries {len(ledger_entries)} regenerate[] records for "
                f"{_CANONICAL_LEDGER_LOGICAL_ID!r} -- exactly one canonical record "
                f"is permitted, duplicates/contradictions are never valid"
            )
        elif len(ledger_entries) == 0 \
                or ledger_entries[0].get("state_class") != "REBUILDABLE_GENERATED" \
                or ledger_entries[0].get("restore_policy") != "always_regenerate_empty":
            errors.append(
                "manifest is missing the canonical ledger regenerate entry "
                f"(regenerate[] with logical_id=={_CANONICAL_LEDGER_LOGICAL_ID!r}, "
                f"state_class=='REBUILDABLE_GENERATED', "
                f"restore_policy=='always_regenerate_empty')"
            )
    return errors


def _validate_canonical_member_semantics(members: list[dict]) -> list[str]:
    """Enforces Lumina's own v1 contract for known canonical logical IDs
    regardless of what the archive's own manifest claims (Section 7) --
    the verifier does not trust arbitrary archive-authored policy for
    state it already knows the correct shape of. Dynamic (per-file)
    custom-tool / tool-audit-log authority semantics are handled
    separately by _validate_authority_bearing_member_schema."""
    errors = []
    by_logical_id = {}
    for m in members:
        if isinstance(m, dict) and isinstance(m.get("logical_id"), str):
            by_logical_id.setdefault(m["logical_id"], m)

    for logical_id, expected in _CANONICAL_MEMBER_POLICY.items():
        m = by_logical_id.get(logical_id)
        if m is None:
            continue  # absence is enforced elsewhere (required-floor checks)
        for field, expected_value in expected.items():
            actual = m.get(field)
            if actual != expected_value:
                errors.append(
                    f"canonical member {logical_id!r} has {field}={actual!r}, but "
                    f"Lumina's v1 contract requires {field}={expected_value!r} regardless "
                    f"of what the archive's own manifest claims"
                )
    return errors


def _validate_authority_bearing_member_schema(members: list[dict]) -> list[str]:
    """Every member type Section 13 names as authority-sensitive (custom
    tools, tool audit, pending actions, Project bindings) plus Project
    chats-linkage (Section 10's own narrower machine-binding-field
    demand) carries potential executable/approval/machine-binding
    authority on Restore -- a TIGHT explicit field allowlist is enforced
    for these member types (not an open schema, via
    _authority_bearing_allowed_fields_for), so a hostile manifest cannot
    smuggle authority through an unrecognized field name (approved,
    autoapprove, destination_authority, autoexecute, owner_authorized,
    ...) that happens not to be one of the fields this module's own
    checks already inspect by name. AGENT-BACKUP-RESTORE-A2R3 established
    this for custom tools/tool_audit_log; AGENT-BACKUP-RESTORE-A2R4
    (Section 11/13) found pending_actions.json/pending_actions_audit.log
    and Project binding.json/chats.json entirely unenforced by it, the
    same class of gap. Dynamic custom-tool members also get their fixed
    policy values enforced here, the same way _validate_canonical_member_
    semantics does for fixed-logical-id members (which can't cover these
    since their logical_id is per-file, state.custom_tools.<relpath>)."""
    errors = []
    for m in members:
        if not isinstance(m, dict):
            continue
        logical_id = m.get("logical_id", "")
        if not isinstance(logical_id, str):
            continue
        allowed = _authority_bearing_allowed_fields_for(logical_id)
        if allowed is None:
            continue

        extra = set(m.keys()) - allowed
        if extra:
            errors.append(
                f"{logical_id!r} carries unrecognized field(s) {sorted(extra)!r} -- "
                f"v1's authority-bearing member schema is a tight allowlist, not an "
                f"open one, so an unexpected field can never smuggle executable/"
                f"approval/machine-binding authority through"
            )

        if logical_id.startswith("state.custom_tools."):
            for field, expected_value in _CUSTOM_TOOL_FIXED_POLICY.items():
                actual = m.get(field)
                if actual != expected_value:
                    errors.append(
                        f"custom tool {logical_id!r} has {field}={actual!r}, but "
                        f"Lumina's v1 contract fixes {field}={expected_value!r} "
                        f"regardless of what the archive's own manifest claims"
                    )
    return errors


def _validate_dynamic_member_policy(members: list[dict]) -> list[str]:
    """Fixed-VALUE enforcement (distinct from the field-ALLOWLIST
    enforcement above) for every dynamic (per-file/per-project) member
    family in _DYNAMIC_MEMBER_POLICY_FAMILIES -- Section 8/9's 'dynamic
    manifest policy is still partially archive-authored' finding
    (AGENT-BACKUP-RESTORE-A2R4): a Project binding's state_class/
    portable/restore_policy/rebind_policy, a Project chats-linkage's
    state_class (including EXCLUDED), or any persona/skill/avatar/voice/
    tool-profile/project-doc's own restore mechanics could all be freely
    redefined by the archive's own manifest with zero enforcement before
    this. Matched by (prefix, suffix); families are mutually exclusive by
    construction (no two entries share both a prefix and a suffix that a
    real logical_id could satisfy simultaneously), so the first match
    wins and further families are not also checked."""
    errors = []
    for m in members:
        if not isinstance(m, dict):
            continue
        logical_id = m.get("logical_id", "")
        if not isinstance(logical_id, str):
            continue
        for prefix, suffix, fixed, _deriver in _DYNAMIC_MEMBER_POLICY_FAMILIES:
            if not (logical_id.startswith(prefix) and logical_id.endswith(suffix)):
                continue
            for field, expected_value in fixed.items():
                actual = m.get(field)
                if actual != expected_value:
                    errors.append(
                        f"member {logical_id!r} has {field}={actual!r}, but Lumina's "
                        f"v1 contract fixes {field}={expected_value!r} for this member "
                        f"family regardless of what the archive's own manifest claims"
                    )
            break
    return errors


def _is_recognized_member(logical_id, archive_path) -> bool:
    """Section 16-19 (A2F5): True iff (logical_id, archive_path)
    TOGETHER match exactly one of Lumina's v1 recognized member
    families -- fixed canonical (exact logical_id, exact archive_path
    equal to that family's own fixed archive_path) or dynamic (the
    family's own archive_path_deriver(logical_id) must equal
    archive_path exactly, not merely both independently look
    plausible -- Section 17). There is no unconstrained physical member
    type in v1 (Section 16): a member matching neither is not a
    'schema-valid but unrecognized' member, it is an INVALID archive."""
    if not isinstance(logical_id, str) or not isinstance(archive_path, str):
        return False
    fixed_member = _CANONICAL_MEMBER_POLICY.get(logical_id)
    if fixed_member is not None:
        return archive_path == fixed_member["archive_path"]
    for prefix, suffix, _fixed, deriver in _DYNAMIC_MEMBER_POLICY_FAMILIES:
        if not (logical_id.startswith(prefix) and logical_id.endswith(suffix)):
            continue
        derived = deriver(logical_id)
        return derived is not None and archive_path == derived
    return False


def _validate_closed_world_membership(dict_members: list[dict]) -> list[str]:
    """AGENT-BACKUP-RESTORE-A2R5 (Section 16-19, BLOCKER): v1 has NO
    unconstrained physical member type. Every physical members[] entry
    must belong to EXACTLY one of Lumina's recognized canonical or
    dynamic member families (_is_recognized_member), or the whole
    archive is invalid -- closing the previous 'known member -> policy
    enforced, unknown member -> schema-only' asymmetry A2R5 exploited to
    inject an uncontrolled state.audit.totally_unconstrained_thing
    member (a schema-valid, otherwise-namespace-legal entry under
    state/audit/ that matched no fixed or dynamic family) which both
    verified valid=True AND inflated the receipt's rebind_required_count
    (Section 20 -- closed automatically here, since a member like that
    can no longer coexist with a valid=True report at all, and the
    receipt's counts are only ever read from a passing report's own
    already-fully-validated member list)."""
    errors = []
    for m in dict_members:
        logical_id = m.get("logical_id")
        archive_path = m.get("archive_path")
        if not isinstance(logical_id, str) or not isinstance(archive_path, str):
            continue  # already reported by the base schema checks
        if not _is_recognized_member(logical_id, archive_path):
            errors.append(
                f"member {logical_id!r} (archive_path {archive_path!r}) does not belong "
                f"to any of Lumina's recognized v1 canonical or dynamic member families "
                f"-- v1 has no unconstrained physical member type; an archive cannot "
                f"introduce a new member kind outside an explicit format version "
                f"evolution plus trusted policy"
            )
    return errors


def _validate_no_excluded_physical_members(dict_members: list[dict]) -> list[str]:
    """Section 12: EXCLUDED is the exclusions[] block's own state -- a
    physical members[] entry (something that WAS archived, with real
    bytes at archive_path) can never legitimately be EXCLUDED at the same
    time (A1 Section B/G: 'EXCLUDED items never appear in members at all,
    they appear only in the exclusions block'). AGENT-BACKUP-RESTORE-A2R4
    found a physical payload member declaring state_class=EXCLUDED (and,
    symmetrically, portable=EXCLUDED) still verified valid=True, since
    _VALID_STATE_CLASSES/_VALID_PORTABILITY's enum-membership check alone
    treats EXCLUDED as a structurally legitimate value -- it legitimately
    IS one, just never on a members[] entry specifically."""
    errors = []
    for m in dict_members:
        logical_id = m.get("logical_id", "<unknown>")
        if m.get("state_class") == "EXCLUDED":
            errors.append(
                f"member {logical_id!r} is a physical archived payload but declares "
                f"state_class=='EXCLUDED' -- EXCLUDED state never appears in members[], "
                f"only in exclusions[]; a physical payload cannot simultaneously claim "
                f"to not exist"
            )
        if m.get("portable") == "EXCLUDED":
            errors.append(
                f"member {logical_id!r} is a physical archived payload but declares "
                f"portable=='EXCLUDED' -- same contradiction as state_class=='EXCLUDED'"
            )
    return errors


def _validate_root_contract(manifest: dict) -> list[str]:
    """Section 5's fixed v1 root contract: an archive cannot substitute
    another backup_mode or hash_algorithm and still be considered a valid
    v1 Agent Backup, regardless of what its own manifest claims
    (AGENT-BACKUP-RESTORE-A2R3: backup_mode='partial', hash_algorithm=
    'md5' manifests were both previously accepted)."""
    errors = []
    if manifest.get("backup_mode") != "full":
        errors.append(
            f"manifest backup_mode must be 'full' (v1 supports no other "
            f"mode): {manifest.get('backup_mode')!r}"
        )
    if manifest.get("hash_algorithm") != "sha256":
        errors.append(
            f"manifest hash_algorithm must be 'sha256' (v1 supports no "
            f"other algorithm): {manifest.get('hash_algorithm')!r}"
        )
    return errors


def _validate_full_backup_required_floor(dict_members: list[dict]) -> list[str]:
    """A 'full' backup_mode archive (v1's only supported mode) must
    contain at minimum the canonical required core -- Section 3:
    AGENT-BACKUP-RESTORE-A2R3 constructed a backup_mode='full' archive
    containing ONLY lumina.db (prefs.json and flight_recorder.db
    removed) and the verifier returned valid:true, because completeness
    was only ever enforced by the BUILD path (build_agent_backup's own
    archive_paths_present floor check) -- never independently by the
    verifier, which is what anything restoring from an untrusted archive
    actually has to rely on."""
    errors = []
    present_ids = {m.get("logical_id") for m in dict_members}
    for logical_id in _REQUIRED_FLOOR_LOGICAL_IDS:
        if logical_id not in present_ids:
            errors.append(
                f"a 'full' backup is missing the canonical required member "
                f"{logical_id!r} -- Lumina's v1 contract requires the full "
                f"required core (lumina.db, flight_recorder.db, prefs.json) "
                f"regardless of what an archive's own manifest claims"
            )
    return errors


def _validate_manifest_schema(manifest) -> list[str]:
    errors = []
    if not isinstance(manifest, dict):
        return ["manifest.json does not decode to a JSON object"]

    for key in ("format", "format_version", "lumina_version", "created_at",
                "source_platform", "source_data_root", "backup_mode",
                "hash_algorithm", "compatibility", "members", "regenerate", "exclusions", "warnings"):
        if key not in manifest:
            errors.append(f"manifest is missing required root field: {key!r}")

    for key, expected_type in _ROOT_FIELD_TYPES.items():
        if key in manifest and not isinstance(manifest[key], expected_type):
            errors.append(
                f"manifest root field {key!r} has wrong type (expected "
                f"{expected_type.__name__}, got {type(manifest[key]).__name__})"
            )

    if manifest.get("format") != MANIFEST_FORMAT:
        errors.append(f"unexpected manifest format: {manifest.get('format')!r}")

    errors.extend(_validate_format_version(manifest.get("format_version")))

    members = manifest.get("members")
    if not isinstance(members, list):
        members = []
    regenerate = manifest.get("regenerate")
    if not isinstance(regenerate, list):
        regenerate = []

    if len(members) == 0:
        errors.append(
            "manifest declares zero members -- a valid Agent Backup can never be empty "
            "(A2R's reproduced 'zero-member backup verifies as valid' failure)"
        )
    elif not any(m.get("logical_id") == "state.databases.lumina_db" for m in members if isinstance(m, dict)):
        errors.append("manifest has no state.databases.lumina_db member -- missing the primary database")

    # Section 12: a non-object entry anywhere in members[]/exclusions[]/
    # warnings[] must invalidate the archive, not be silently dropped by
    # a `isinstance(x, dict)` filter downstream while a raw len() of the
    # unfiltered list still counts it (AGENT-BACKUP-RESTORE-A2R3:
    # appended a scalar to members[] -- verification silently ignored it
    # while a receipt built from the raw list still counted it).
    for m in members:
        if not isinstance(m, dict):
            errors.append(f"a members[] entry is not an object: {m!r}")
    exclusions_field = manifest.get("exclusions")
    if isinstance(exclusions_field, list):
        for e in exclusions_field:
            if not isinstance(e, dict):
                errors.append(f"an exclusions[] entry is not an object: {e!r}")
    warnings_field = manifest.get("warnings")
    if isinstance(warnings_field, list):
        for w in warnings_field:
            if not isinstance(w, str):
                errors.append(f"a warnings[] entry is not a string: {w!r}")

    dict_members = [m for m in members if isinstance(m, dict)]

    for m in dict_members:
        logical_id = m.get("logical_id", "<unknown>")
        for field, expected in (("logical_id", str), ("state_class", str), ("content_kind", str),
                                 ("archive_path", str), ("sha256", str), ("required", bool),
                                 ("portable", str), ("restore_policy", str)):
            if field not in m:
                errors.append(f"member {logical_id!r} missing required field {field!r}")
            elif not isinstance(m[field], expected):
                errors.append(f"member {logical_id!r} field {field!r} has wrong type "
                              f"(expected {expected.__name__}, got {type(m[field]).__name__})")
        if "size" not in m:
            errors.append(f"member {logical_id!r} missing required field 'size'")
        elif not isinstance(m["size"], int) or isinstance(m["size"], bool) or m["size"] < 0:
            errors.append(f"member {logical_id!r} has an invalid size: {m.get('size')!r}")
        if isinstance(m.get("sha256"), str) and not _SHA256_RE.match(m["sha256"]):
            errors.append(f"member {logical_id!r} has a malformed sha256: {m['sha256']!r}")
        if isinstance(m.get("state_class"), str) and m["state_class"] not in _VALID_STATE_CLASSES:
            errors.append(f"member {logical_id!r} has an unknown state_class: {m['state_class']!r}")
        if isinstance(m.get("portable"), str) and m["portable"] not in _VALID_PORTABILITY:
            errors.append(f"member {logical_id!r} has an unknown portable value: {m['portable']!r}")

    for r in regenerate:
        if not isinstance(r, dict):
            errors.append(f"a regenerate[] entry is not an object: {r!r}")
            continue
        rid = r.get("logical_id", "<unknown>")
        for field in ("logical_id", "state_class", "restore_policy"):
            if field not in r:
                errors.append(f"regenerate entry {rid!r} missing required field {field!r}")
        for forbidden in ("archive_path", "sha256", "size"):
            if forbidden in r:
                errors.append(
                    f"regenerate entry {rid!r} must not carry {forbidden!r} -- its content "
                    f"is never archived at all, that is the whole point of this being a "
                    f"regenerate entry rather than an optional members entry"
                )

    errors.extend(_check_path_and_identity_rules(dict_members))
    errors.extend(_validate_root_contract(manifest))
    errors.extend(_validate_full_backup_required_floor(dict_members))
    errors.extend(_validate_canonical_policy_records(manifest))
    errors.extend(_validate_canonical_member_semantics(dict_members))
    errors.extend(_validate_authority_bearing_member_schema(dict_members))
    errors.extend(_validate_dynamic_member_policy(dict_members))
    errors.extend(_validate_no_excluded_physical_members(dict_members))
    errors.extend(_validate_closed_world_membership(dict_members))

    return errors


def _verify_prefs_payload(data: bytes) -> tuple[bool, str]:
    """Independently proves `data` is a genuine Lumina preferences
    document -- valid UTF-8, valid JSON, a top-level JSON object -- never
    trusting a matching hash/size alone (AGENT-BACKUP-RESTORE-A2R3:
    prefs.json = 'not-json' with a correct hash/size still verified
    valid=True). No specific key is required: core/persistence.py's own
    load() merges any subset of keys onto _defaults and tolerates any of
    them being absent, so requiring a specific key here would reject a
    genuinely valid prefs.json this particular machine simply hasn't
    written yet."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        return False, f"prefs.json is not valid UTF-8: {e}"
    try:
        parsed = _strict_json_loads(text)
    except ValueError as e:
        return False, f"prefs.json is not valid JSON: {e}"
    if not isinstance(parsed, dict):
        return False, (
            f"prefs.json must be a JSON object at the top level, got "
            f"{type(parsed).__name__}"
        )
    return True, ""


def _verify_role_schema(conn: sqlite3.Connection, required_tables: dict) -> tuple[bool, str]:
    """Checks that `conn` (already known to be a valid, quick_check==ok
    SQLite database) additionally carries the specific tables/columns a
    given Lumina database role is known to always create -- role-specific
    schema identity, not merely 'any healthy SQLite file' (Section 4)."""
    try:
        actual_tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.Error as e:
        return False, f"could not enumerate tables: {e}"
    missing_tables = set(required_tables) - actual_tables
    if missing_tables:
        return False, f"missing required table(s): {sorted(missing_tables)}"
    for table, required_columns in required_tables.items():
        try:
            actual_columns = {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        except sqlite3.Error as e:
            return False, f"could not inspect columns of table {table!r}: {e}"
        missing_columns = required_columns - actual_columns
        if missing_columns:
            return False, f"table {table!r} is missing required column(s): {sorted(missing_columns)}"
    return True, ""


# Maps a canonical logical_id to the role-schema fingerprint
# _verify_sqlite_payload should additionally enforce for it -- only
# lumina_db and flight_recorder_db carry content_kind=='sqlite_database'
# in v1, so no other logical_id needs an entry here.
_SQLITE_ROLE_BY_LOGICAL_ID = {
    "state.databases.lumina_db": "lumina_db",
    "state.telemetry.flight_recorder_db": "flight_recorder_db",
}


def _verify_sqlite_payload(data: bytes, role: str | None = None) -> tuple[bool, str]:
    """Independently proves `data` is actually a valid SQLite database,
    never trusting manifest-recorded metadata (AGENT-BACKUP-RESTORE-A2R2:
    'staged DB corrupted after the integrity check, manifest still says
    integrity_check=ok, verify_agent_backup still returns valid=True').

    AGENT-BACKUP-RESTORE-A2R9 (Section 8-9): validates `data` through a
    fresh in-memory SQLite database (Connection.deserialize()) rather
    than writing it to a throwaway temp file and reopening that file by
    name -- the preferred mechanism Section 9 asks for, source-vetted as
    genuinely supported by this runtime's sqlite3/SQLite build (not
    assumed). This also means the exact bytes this function is handed are
    the exact bytes quick_check/role-schema validation runs against, with
    no filesystem round-trip of any kind in between. A non-SQLite byte
    string with a matching hash is still an invalid Agent Backup --
    checked by magic header before ever asking SQLite to open it.

    When `role` is given ('lumina_db' or 'flight_recorder_db'), this ALSO
    independently proves the payload matches that role's own schema
    fingerprint (Section 4: AGENT-BACKUP-RESTORE-A2R3 substituted an
    unrelated, structurally-healthy SQLite database -- valid quick_check,
    matching hash/size, just the wrong content -- at state/databases/
    lumina.db and it still verified valid=True; a passing quick_check
    proves 'this is some SQLite database', not 'this is Lumina's own')."""
    if not data.startswith(_SQLITE_MAGIC):
        return False, "archived bytes do not have a SQLite database header"

    conn = sqlite3.connect(":memory:")
    try:
        try:
            conn.deserialize(data)
        except sqlite3.Error as e:
            return False, f"archived SQLite payload could not be opened: {e}"
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                return False, f"archived SQLite payload failed quick_check: {result}"
            if role == "lumina_db":
                ok, reason = _verify_role_schema(conn, _LUMINA_DB_REQUIRED_TABLES)
                if not ok:
                    return False, (
                        f"archived bytes do not match Lumina's primary-database "
                        f"schema ({reason}) -- a healthy SQLite file that isn't "
                        f"actually lumina.db is still an invalid Agent Backup"
                    )
            elif role == "flight_recorder_db":
                ok, reason = _verify_role_schema(conn, _FLIGHT_RECORDER_DB_REQUIRED_TABLES)
                if not ok:
                    return False, (
                        f"archived bytes do not match Flight Recorder's schema "
                        f"({reason}) -- a healthy SQLite file that isn't actually "
                        f"flight_recorder.db is still an invalid Agent Backup"
                    )
        except sqlite3.Error as e:
            return False, f"archived SQLite payload failed quick_check: {e}"
        return True, ""
    finally:
        conn.close()


# AGENT-BACKUP-RESTORE-A2R7 (Section 20): a valid ZIP central directory
# says nothing about what comes AFTER the archive's own End-Of-Central-
# Directory (EOCD) record. Most ZIP readers -- Python's zipfile module
# included -- locate the EOCD by searching, and neither complains about
# nor even notices extra bytes appended past a structurally complete,
# valid archive; a 23-byte trailing append passed this module's own
# strict verifier entirely undetected. Lumina's v1 backup format defines
# no use whatsoever for a ZIP comment or any trailing data: a genuinely
# build_agent_backup-produced archive's raw byte length is always EXACTLY
# EOCD_offset + 22 (the fixed-size EOCD record) with a zero-length
# comment, since this module's own zipfile.ZipFile(..., "w") call never
# sets one.
_EOCD_SIGNATURE = b"PK\x05\x06"
_EOCD_FIXED_SIZE = 22


def _check_no_trailing_zip_bytes(raw_bytes: bytes) -> str | None:
    """Returns an error message if `raw_bytes` carries anything beyond a
    single EOCD record with a zero-length comment, or None if the raw
    bytes exactly account for themselves. Searches for the LAST
    occurrence of the EOCD signature (the same direction real ZIP
    readers search from), matching zipfile's own EOCD-location
    semantics rather than inventing a stricter parse zipfile itself
    wouldn't agree describes the same file."""
    idx = raw_bytes.rfind(_EOCD_SIGNATURE)
    if idx == -1:
        return "no End-Of-Central-Directory record found"
    if idx + _EOCD_FIXED_SIZE > len(raw_bytes):
        return "truncated End-Of-Central-Directory record"
    comment_length = int.from_bytes(raw_bytes[idx + 20:idx + 22], "little")
    expected_total = idx + _EOCD_FIXED_SIZE + comment_length
    if expected_total != len(raw_bytes):
        return (
            f"archive contains {len(raw_bytes) - expected_total} byte(s) of "
            f"unaccounted trailing data beyond the End-Of-Central-Directory "
            f"record and its declared comment -- Lumina's v1 format defines "
            f"no use for trailing bytes"
        )
    if comment_length != 0:
        return (
            f"archive carries a {comment_length}-byte ZIP comment -- not part "
            f"of Lumina's v1 backup format"
        )
    return None


# AGENT-BACKUP-RESTORE-A2R8/A2F8 (Section 19-26, BLOCKER B4): a valid ZIP
# central directory proves every DECLARED member's bytes are present and
# uncorrupted, but says nothing about whether the archive's own PHYSICAL
# layout is the single, unambiguous structure Lumina's own builder emits.
# A2R8 proved strict verification accepted a leading-byte prefix (a
# self-extracting-stub-shaped attack) and a second, complete ZIP archive
# concatenated in front of Lumina's own, because Python's zipfile module
# (like most real ZIP readers) locates the End-Of-Central-Directory record
# by searching BACKWARD from the end of the file and transparently adjusts
# every entry's `header_offset` for whatever came before it -- exactly the
# behavior that makes a prepended stub or a leading archive invisible to a
# check that only inspects zipfile's already-parsed member list. v1 closes
# this by walking the archive's own physical local file headers directly
# (using zipfile's own prefix-ADJUSTED header_offset values -- confirmed
# empirically to reflect true physical position, not the raw value stored
# in the central directory) and requiring them to exactly tile
# [0, cd_offset) with the first one starting at byte 0: no leading prefix,
# no concatenated archive, no unreferenced extra local record, no gap, no
# overlap. This is deliberately NOT a general ZIP structural validator --
# it validates only the narrow physical subset zipfile.ZipFile(...,
# ZIP_DEFLATED) actually emits when writing to a real, seekable file (no
# multi-disk splitting, no ZIP64, no streaming data descriptors -- see
# Section 20's source-vetted confirmation of build_agent_backup's own
# emitted grammar), and is not intended to (nor tries to) accept every
# ZIP variant Python's own zipfile module would otherwise tolerate.
_LOCAL_HEADER_SIGNATURE = b"PK\x03\x04"
_LOCAL_HEADER_STRUCT = "<4sHHHHHIIIHH"
_LOCAL_HEADER_SIZE = struct.calcsize(_LOCAL_HEADER_STRUCT)
assert _LOCAL_HEADER_SIZE == 30
_EOCD_STRUCT = "<4sHHHHII"
_ZIP64_SENTINEL_32 = 0xFFFFFFFF
_ZIP64_SENTINEL_16 = 0xFFFF
_DATA_DESCRIPTOR_FLAG = 0x08

# AGENT-BACKUP-RESTORE-A2F10 (BLOCKER B1, Section 3-7): the exact,
# closed v1 ZIP LANGUAGE -- not merely "local agrees with central" (an
# archive can satisfy that and still describe a structure Lumina's own
# builder could never emit, e.g. both records agreeing on ZIP_STORED or
# on a reserved flag bit). Source-vetted empirically (never assumed)
# against the actual builder (core/agent_backup.py's own
# zipfile.ZipFile(..., zipfile.ZIP_DEFLATED) call in
# _build_agent_backup_core, which unconditionally sets
# zinfo.compress_type = zipfile.ZIP_DEFLATED for every member including
# manifest.json) across ASCII and Unicode filenames, a zero-byte
# payload, binary payloads, an SQLite payload, and both highly
# compressible and incompressible payloads: every real member came back
# with general-purpose flags of either 0x0000 or exactly 0x0800 (the
# UTF-8 filename bit zipfile itself sets for a non-ASCII name), method
# ZIP_DEFLATED in both local and central records, and a ZERO-length
# extra field in both local and central records for every single
# member. There is no legitimate builder path to ZIP_STORED, to any
# other flag bit (encryption, streaming data descriptor, DEFLATE option
# bits, strong encryption, reserved bits), or to a non-empty extra
# field -- so strict v1 verification requires membership in this exact,
# closed set, not merely internal local/central agreement.
_ALLOWED_COMPRESSION_METHODS = frozenset({zipfile.ZIP_DEFLATED})
_ALLOWED_GENERAL_PURPOSE_FLAGS = frozenset({0x0000, 0x0800})

# AGENT-BACKUP-RESTORE-A2F11 (P0, Section 21-27, repairing the A2R11 P0
# ZIP-canonicalization blocker): the ONE canonical v1 member metadata
# contract, for every parser-, disk-, and entry-type-sensitive field the
# prior pass's closed compression/flags/extra-field language (above)
# still left unconstrained. A2R11 found strict verification accepted
# builder-impossible central-directory state -- version needed 63, disk
# number start 1, Unix symlink external attributes -- because nothing
# checked these fields against what the real builder actually emits.
# Source-vetted empirically (never assumed, Section 22) by building a
# real archive through this module's own zipfile.ZipFile(...,
# ZIP_DEFLATED, allowZip64=False) path and inspecting the resulting
# infolist(): create_system=3 (host-platform-dependent in zipfile's own
# ZipInfo.__init__ -- 0 on win32, 3 elsewhere -- made explicit here so
# v1's format semantics never accidentally depend on whichever OS
# produced the archive, Section 22/27), create_version=extract_version=20
# (zipfile's own DEFAULT_VERSION for a non-ZIP64, non-encrypted,
# DEFLATE-or-STORED archive), volume=0 (disk number start -- v1 is never
# multi-disk), internal_attr=0, external_attr=(S_IFREG|0o644)<<16 (an
# EXPLICIT regular file, rw-r--r--, never a type-less mode, symlink,
# directory, or other special-file bits -- AGENT-BACKUP-RESTORE-A2F12
# corrected the original 0o644<<16, which encoded only permission bits with
# no file-type bit at all). This candidate has
# never shipped (Section 27) -- finalizing ONE canonical value now,
# shared by builder and verifier alike, closes the ambiguity rather than
# growing the verifier's own allowlist to match whatever a given host
# happens to produce.
_V1_ZIP_CREATE_SYSTEM = 3  # Unix -- explicit, independent of sys.platform
_V1_ZIP_VERSION = 20  # create_version == extract_version; DEFAULT_VERSION
# AGENT-BACKUP-RESTORE-A2F12 (P0-2 / A2R12): 0o644 alone is PERMISSION bits
# only -- it carries no file-TYPE bits, so a bare `0o644 << 16` external_attr
# is, by POSIX stat(2) semantics, a type-less mode (S_ISREG of it is False).
# A2R12 confirmed this let mutated archives carry symlink/directory/FIFO/
# socket/device external attributes with permissions rewritten to 0644 and
# still pass strict verification, since nothing upstream of the byte-equality
# check below ever required the S_IFREG bit specifically. stat.S_IFREG is
# the standard-library regular-file type constant (0o100000); OR-ing it into
# the high word before the existing <<16 shift makes every v1 member
# canonically and explicitly an ordinary regular file (0100644) rather than
# merely "permissions 0644 on an unspecified type" -- closing the gap without
# touching the low (DOS-attribute) word, which stays 0 either way.
_V1_ZIP_EXTERNAL_ATTR = (stat.S_IFREG | 0o644) << 16  # regular file, rw-r--r--
_V1_ZIP_INTERNAL_ATTR = 0
_V1_ZIP_VOLUME = 0  # disk number start -- v1 is never a multi-disk archive


def _make_v1_zipinfo(archive_path: str) -> "zipfile.ZipInfo":
    """The ONE canonical ZipInfo construction this module's builder ever
    uses (AGENT-BACKUP-RESTORE-A2F11, Section 21-23) -- every physical
    member, manifest.json included (Section 27: manifest.json was
    previously written via a bare-string zf.writestr() call, which left
    zipfile to fill in ITS OWN default external_attr, 0o600 << 16,
    rather than this module's own explicit 0o644 << 16 -- a real,
    source-vetted discrepancy this pass closes rather than special-cases
    around), gets IDENTICAL create_system/create_version/extract_version/
    external_attr/internal_attr/volume/comment/extra -- never left to
    whatever zipfile's own or the host platform's default happens to be
    (Section 22). CRC-32, compressed size, and uncompressed size are
    deliberately NOT set here -- zipfile computes those from the
    member's actual content at write time, and always has; this helper
    only fixes the metadata fields that were previously left to drift."""
    zinfo = zipfile.ZipInfo(archive_path, date_time=time.localtime()[:6])
    zinfo.compress_type = zipfile.ZIP_DEFLATED
    zinfo.create_system = _V1_ZIP_CREATE_SYSTEM
    zinfo.create_version = _V1_ZIP_VERSION
    zinfo.extract_version = _V1_ZIP_VERSION
    zinfo.external_attr = _V1_ZIP_EXTERNAL_ATTR
    zinfo.internal_attr = _V1_ZIP_INTERNAL_ATTR
    zinfo.volume = _V1_ZIP_VOLUME
    zinfo.comment = b""
    zinfo.extra = b""
    return zinfo


def _parse_local_header(raw_bytes: bytes, offset: int):
    """Parses the physical local file header at `offset`, or returns None
    on ANY malformed input (out-of-range offset, truncated header,
    truncated filename/extra fields, bad signature) -- never raises
    (Section 25: malformed ZIP structure must return a structured invalid
    result, never an uncaught struct.error/IndexError). Returns a dict
    with `flags`, `compression`, `comp_size`, `uncomp_size`, `fname`
    (raw, still-encoded bytes -- NOT decoded, so the caller can compare
    against the central directory's own filename using the exact same
    utf-8-or-cp437 rule zipfile itself used to decode it), `extra_len`
    (AGENT-BACKUP-RESTORE-A2F10, Section 7 -- the local header's own
    extra-field length, previously parsed and folded into `header_size`
    only, never checked in its own right), `version_needed`
    (AGENT-BACKUP-RESTORE-A2F11, Section 24 -- previously parsed and
    immediately discarded as `_version_needed`, never compared against
    the central directory's own extract_version or the canonical v1
    value), and `header_size` (the total physical size of this local
    record's header + filename + extra fields, NOT including the
    compressed payload that follows it)."""
    if offset < 0 or offset + _LOCAL_HEADER_SIZE > len(raw_bytes):
        return None
    try:
        (sig, version_needed, flags, compression, _mtime, _mdate, crc32,
         comp_size, uncomp_size, fname_len, extra_len) = struct.unpack(
            _LOCAL_HEADER_STRUCT, raw_bytes[offset:offset + _LOCAL_HEADER_SIZE])
    except struct.error:
        return None
    if sig != _LOCAL_HEADER_SIGNATURE:
        return None
    header_size = _LOCAL_HEADER_SIZE + fname_len + extra_len
    if offset + header_size > len(raw_bytes):
        return None
    fname = raw_bytes[offset + _LOCAL_HEADER_SIZE:offset + _LOCAL_HEADER_SIZE + fname_len]
    return {
        "flags": flags, "compression": compression, "crc32": crc32,
        "comp_size": comp_size, "uncomp_size": uncomp_size, "fname": fname,
        "extra_len": extra_len, "header_size": header_size,
        "version_needed": version_needed,
    }


def _validate_zip_physical_structure(raw_bytes: bytes, infolist: list) -> list[str]:
    """Section 21-24: independently walks the archive's own physical local
    file headers (never trusting zipfile's already-decoded member list
    alone) and requires them to exactly tile [0, cd_offset) with the
    first one starting at byte 0 -- closing the leading-prefix and
    concatenated-archive attacks in one check (Section 21) -- plus
    per-member local/central-directory agreement on filename, compression
    method, flags, and compressed size (Section 22-23), so a future
    Restore (or this verifier's own zipfile-based reads) cannot be
    induced to interpret a different physical member than the one the
    central directory describes. Never raises (Section 25) -- every
    malformed-input path returns a descriptive error string instead.

    Returns a list of error strings (empty if the physical layout is
    exactly the single, unambiguous structure Lumina's own builder
    emits)."""
    errors = []
    idx = raw_bytes.rfind(_EOCD_SIGNATURE)
    if idx == -1 or idx + _EOCD_FIXED_SIZE > len(raw_bytes):
        return ["could not locate a well-formed End-Of-Central-Directory record "
                "for physical structure validation"]
    try:
        (_sig, disk_num, disk_with_cd, entries_this_disk, total_entries,
         cd_size, cd_offset) = struct.unpack(_EOCD_STRUCT, raw_bytes[idx:idx + 20])
    except struct.error as e:
        return [f"could not parse End-Of-Central-Directory record: {e}"]

    if disk_num != 0 or disk_with_cd != 0 or entries_this_disk != total_entries:
        return ["archive declares a multi-disk/split ZIP layout -- not part of Lumina's v1 format"]

    if cd_offset == _ZIP64_SENTINEL_32 or cd_size == _ZIP64_SENTINEL_32 \
            or total_entries == _ZIP64_SENTINEL_16:
        return ["archive requires ZIP64 extensions -- not part of Lumina's v1 format"]

    if cd_offset + cd_size != idx:
        errors.append(
            f"central directory (offset {cd_offset}, size {cd_size}) does not end exactly "
            f"where the End-Of-Central-Directory record begins (byte {idx}) -- unaccounted "
            f"gap or overlap between the central directory and the EOCD record"
        )

    if total_entries != len(infolist):
        errors.append(
            f"End-Of-Central-Directory declares {total_entries} member(s) but "
            f"{len(infolist)} were actually read from the central directory"
        )

    if not infolist:
        errors.append("archive has no members at all")
        return errors

    by_offset = sorted(infolist, key=lambda i: i.header_offset)
    if by_offset[0].header_offset != 0:
        errors.append(
            f"first local file header does not start at byte 0 (starts at "
            f"{by_offset[0].header_offset}) -- Lumina's v1 format never has a leading "
            f"prefix, a self-extracting stub, or a prior concatenated archive before its "
            f"own first member"
        )

    cursor = by_offset[0].header_offset
    for info in by_offset:
        if info.header_offset != cursor:
            errors.append(
                f"unaccounted gap, overlap, or unreferenced local record in the archive's "
                f"physical layout before {info.filename!r}: expected the next local file "
                f"header at byte {cursor}, the central directory declares it at "
                f"{info.header_offset}"
            )
            cursor = info.header_offset  # do not cascade the same gap into every later entry

        local = _parse_local_header(raw_bytes, info.header_offset)
        if local is None:
            errors.append(
                f"no valid local file header found at the offset the central directory "
                f"declares for {info.filename!r} (byte {info.header_offset})"
            )
            continue

        if local["flags"] & _DATA_DESCRIPTOR_FLAG or info.flag_bits & _DATA_DESCRIPTOR_FLAG:
            errors.append(
                f"member {info.filename!r} uses a streaming data descriptor -- not part of "
                f"the structure Lumina's own builder emits (it always writes to a seekable "
                f"file with sizes known up front)"
            )

        # AGENT-BACKUP-RESTORE-A2F10 (BLOCKER B1, Section 3-7): local ==
        # central agreement is necessary but not sufficient -- both
        # records could agree on a structure Lumina's own v1 builder
        # never emits (ZIP_STORED, a reserved/encryption/strong-
        # encryption/DEFLATE-option flag bit, a non-empty extra field).
        # Require membership in the source-vetted, closed builder-
        # emitted set (see _ALLOWED_COMPRESSION_METHODS/_ALLOWED_
        # GENERAL_PURPOSE_FLAGS's own module-level comment) independently
        # of the equality checks below, so two mutually-agreeing but
        # builder-impossible records are rejected either way.
        if info.compress_type not in _ALLOWED_COMPRESSION_METHODS:
            errors.append(
                f"member {info.filename!r} declares compression method {info.compress_type} "
                f"in the central directory -- Lumina's v1 builder only ever emits "
                f"ZIP_DEFLATED ({zipfile.ZIP_DEFLATED}); no other method is part of this format"
            )
        if local["compression"] not in _ALLOWED_COMPRESSION_METHODS:
            errors.append(
                f"member {info.filename!r} declares compression method {local['compression']} "
                f"in its own local file header -- Lumina's v1 builder only ever emits "
                f"ZIP_DEFLATED ({zipfile.ZIP_DEFLATED}); no other method is part of this format"
            )
        if info.flag_bits not in _ALLOWED_GENERAL_PURPOSE_FLAGS:
            errors.append(
                f"member {info.filename!r} declares general-purpose flags "
                f"{info.flag_bits:#06x} in the central directory -- not part of the set "
                f"Lumina's v1 builder ever emits "
                f"({', '.join(f'{f:#06x}' for f in sorted(_ALLOWED_GENERAL_PURPOSE_FLAGS))})"
            )
        if local["flags"] not in _ALLOWED_GENERAL_PURPOSE_FLAGS:
            errors.append(
                f"member {info.filename!r} declares general-purpose flags "
                f"{local['flags']:#06x} in its own local file header -- not part of the set "
                f"Lumina's v1 builder ever emits "
                f"({', '.join(f'{f:#06x}' for f in sorted(_ALLOWED_GENERAL_PURPOSE_FLAGS))})"
            )
        if info.extra:
            errors.append(
                f"member {info.filename!r} carries a {len(info.extra)}-byte central-directory "
                f"extra field -- Lumina's v1 builder never writes one"
            )
        if local.get("extra_len", 0):
            errors.append(
                f"member {info.filename!r} carries a {local['extra_len']}-byte local-header "
                f"extra field -- Lumina's v1 builder never writes one"
            )

        # AGENT-BACKUP-RESTORE-A2F11 (P0, Section 21-30, repairing the
        # A2R11 P0 ZIP-canonicalization blocker): local==central agreement
        # (checked further below, as it always has been) is still not
        # sufficient on its own -- A2R11 proved strict verification
        # accepted BUILDER-IMPOSSIBLE state that both records nonetheless
        # AGREED on: version needed 63 (neither this module's canonical
        # 20 nor a genuine ZIP64 archive, which is independently rejected
        # elsewhere), disk number start 1 (v1 is never multi-disk), and
        # Unix symlink external file attributes (v1 payload members are
        # always regular files). Each check below independently requires
        # membership in the ONE canonical value _make_v1_zipinfo actually
        # emits (_V1_ZIP_*), exactly like the compression-method/flags
        # closure above -- never merely "local agrees with central".
        if info.create_version != _V1_ZIP_VERSION:
            errors.append(
                f"member {info.filename!r} declares create_version {info.create_version} in "
                f"the central directory -- Lumina's v1 builder only ever emits "
                f"{_V1_ZIP_VERSION}"
            )
        if info.extract_version != _V1_ZIP_VERSION:
            errors.append(
                f"member {info.filename!r} declares a version needed to extract of "
                f"{info.extract_version} in the central directory -- Lumina's v1 builder only "
                f"ever emits {_V1_ZIP_VERSION} (never ZIP64's 45, never any other value)"
            )
        if local.get("version_needed") != _V1_ZIP_VERSION:
            errors.append(
                f"member {info.filename!r} declares a version needed to extract of "
                f"{local.get('version_needed')} in its own local file header -- Lumina's v1 "
                f"builder only ever emits {_V1_ZIP_VERSION}"
            )
        if local.get("version_needed") != info.extract_version:
            errors.append(
                f"member {info.filename!r} declares a version needed to extract of "
                f"{info.extract_version} in the central directory but "
                f"{local.get('version_needed')} in its own local file header"
            )
        if info.create_system != _V1_ZIP_CREATE_SYSTEM:
            errors.append(
                f"member {info.filename!r} declares create_system {info.create_system} in the "
                f"central directory -- Lumina's v1 builder only ever emits "
                f"{_V1_ZIP_CREATE_SYSTEM} (Unix), independent of whatever host platform built "
                f"this archive"
            )
        if info.volume != _V1_ZIP_VOLUME:
            errors.append(
                f"member {info.filename!r} declares disk number start {info.volume} in the "
                f"central directory -- Lumina's v1 format is never a multi-disk/split archive"
            )
        if info.internal_attr != _V1_ZIP_INTERNAL_ATTR:
            errors.append(
                f"member {info.filename!r} declares internal file attributes "
                f"{info.internal_attr} in the central directory -- Lumina's v1 builder only "
                f"ever emits {_V1_ZIP_INTERNAL_ATTR}"
            )
        if info.external_attr != _V1_ZIP_EXTERNAL_ATTR:
            errors.append(
                f"member {info.filename!r} declares external file attributes "
                f"{info.external_attr:#010x} in the central directory -- Lumina's v1 builder "
                f"only ever emits {_V1_ZIP_EXTERNAL_ATTR:#010x} (a regular file, rw-r--r--, "
                f"never a symlink, directory, or other special-file mode)"
            )
        if info.comment:
            errors.append(
                f"member {info.filename!r} carries a {len(info.comment)}-byte central-directory "
                f"file comment -- Lumina's v1 builder never writes one"
            )

        # AGENT-BACKUP-RESTORE-A2R9 (Section 21-24, BLOCKER B4): strict v1
        # previously agreed the local/central directory only on filename,
        # compression method, and compressed size -- CRC-32, uncompressed
        # size, and the FULL general-purpose flags word (not merely the
        # single data-descriptor bit checked above) were never compared,
        # so a local header could disagree with the central directory on
        # any of those three fields and still verify valid=True. A2R9
        # also confirmed live that a genuinely Unicode member's flag_bits
        # legitimately carries 0x800 (UTF-8 filename) -- there is no
        # single fixed flag_bits value every builder-emitted member
        # shares, so the correct contract is EQUALITY between local and
        # central for whatever value each member's own local/central
        # records actually declare (Section 24), not a hardcoded expected
        # constant.
        if local["flags"] != info.flag_bits:
            errors.append(
                f"member {info.filename!r} declares general-purpose flags "
                f"{info.flag_bits:#06x} in the central directory but "
                f"{local['flags']:#06x} in its own local file header"
            )

        if local["crc32"] != info.CRC:
            errors.append(
                f"member {info.filename!r} declares CRC-32 {info.CRC:#010x} in the central "
                f"directory but {local['crc32']:#010x} in its own local file header"
            )

        if local["uncomp_size"] != info.file_size:
            errors.append(
                f"member {info.filename!r} declares uncompressed size {info.file_size} in "
                f"the central directory but {local['uncomp_size']} in its own local file header"
            )

        expected_fname = info.filename.encode("utf-8") if (info.flag_bits & 0x800) \
            else info.filename.encode("cp437", "replace")
        if local["fname"] != expected_fname:
            errors.append(
                f"local file header filename does not match the central directory's "
                f"filename for {info.filename!r} -- a reader could be induced to interpret "
                f"a different physical member than the one the central directory describes"
            )

        if local["compression"] != info.compress_type:
            errors.append(
                f"member {info.filename!r} declares compression method {info.compress_type} "
                f"in the central directory but {local['compression']} in its own local file "
                f"header"
            )

        if local["comp_size"] != info.compress_size:
            errors.append(
                f"member {info.filename!r} declares compressed size {info.compress_size} in "
                f"the central directory but {local['comp_size']} in its own local file header"
            )

        cursor = info.header_offset + local["header_size"] + local["comp_size"]

    if cursor != cd_offset:
        errors.append(
            f"the last accounted-for local record ends at byte {cursor}, but the central "
            f"directory begins at byte {cd_offset} -- unaccounted gap or unreferenced data "
            f"between the archive's payload members and its central directory"
        )

    return errors


def _raw_bytes_for_trailing_check(archive_source) -> bytes | None:
    """Best-effort raw-byte access for the trailing-bytes check above --
    never raises; a None return means the check is simply skipped for
    this call (the normal zipfile-based checks elsewhere already handle
    an unreadable/malformed source)."""
    try:
        if isinstance(archive_source, (bytes, bytearray)):
            return bytes(archive_source)
        if hasattr(archive_source, "getvalue"):
            return archive_source.getvalue()
        with open(archive_source, "rb") as f:
            return f.read()
    except OSError:
        return None


def verify_agent_backup(archive_source, resident_bytes_hint: int = 0) -> dict:
    """Strictly checks an Agent Backup archive against Lumina's own
    backup-format contract: valid zip, valid+complete manifest schema,
    every physical zip member declared in the manifest exactly once and
    vice versa (no undeclared extras, no missing declared members, no
    duplicate zip entries), every declared member's sha256/size matching
    its actual archived bytes, every SQLite-typed member independently
    re-proven to be a genuinely valid SQLite database (not merely a byte
    string whose hash happens to match), the path-safety/Unicode-namespace
    rules from Section 8/9 (no absolute paths, no '..', no
    normalization/case-fold collisions, no physical ledger.db, nothing
    outside the allowed v1 namespace), the physical ZIP structure rules
    from Section 19-26 (AGENT-BACKUP-RESTORE-A2R8/A2F8 -- no leading
    prefix, no concatenated archive, no unreferenced local record), and
    Lumina's own canonical v1 semantics for known logical IDs and policy
    records (Section 7) -- not merely archive shape.

    `archive_source` is anything zipfile.ZipFile itself accepts: a path
    string, or an already-materialized bytes-like/file-like object (e.g.
    io.BytesIO) -- used by _capture_and_verify_archive (AGENT-BACKUP-
    RESTORE-A2R8/A2F8, BLOCKER B1) to verify an already-captured archive's
    EXACT bytes without a second, separate reopen of its source pathname.

    `resident_bytes_hint` (AGENT-BACKUP-RESTORE-A2F11, Section 3/10/11):
    optional -- bytes this call's OWN caller already knows are resident
    in this process for reasons OTHER than archive_source itself (e.g. a
    build's own already-collected new-member payload, still resident
    while that same build now verifies its existing prior destination).
    Defaults to 0, correct for every standalone caller; only
    _capture_and_verify_archive's own internal build-pipeline callers
    pass a nonzero value. Feeds the host-safe decompression budget
    (_determine_verification_payload_budget) -- never expands it.

    Returns {'valid': bool, 'errors': [...], 'manifest': dict | None,
    'verified_sqlite_members': [archive_path, ...]}. Never raises itself
    -- including on a malformed format_version, non-UTF-8 zip member
    names, or otherwise structurally broken/corrupt input, all of which
    are reported as error entries rather than an uncaught exception."""
    try:
        return _verify_agent_backup_inner(archive_source, resident_bytes_hint=resident_bytes_hint)
    except Exception as e:
        # Belt-and-suspenders: the specific handling below already covers
        # every known malformed-input case, but "never raises" is this
        # function's explicit contract (Section 7/9) -- any genuinely
        # unanticipated failure still comes back structured, not as an
        # uncaught exception from a verifier being handed hostile input.
        return {"valid": False, "errors": [f"unexpected error verifying archive: {e}"],
                "manifest": None, "verified_sqlite_members": []}


def _verify_agent_backup_inner(archive_source, resident_bytes_hint: int = 0) -> dict:
    """archive_source is anything zipfile.ZipFile itself accepts: a path
    string (the public verify_agent_backup's own contract), or an
    already-materialized file-like object (io.BytesIO) -- used by
    build_backup_receipt (Section 16/17) to verify the EXACT SAME bytes
    it hashes/sizes, both bound to a single os.open() call, rather than
    reopening the archive_path a second time by name.

    `resident_bytes_hint` -- see verify_agent_backup's own docstring
    (AGENT-BACKUP-RESTORE-A2F11, Section 3/10/11)."""
    errors = []
    manifest = None
    verified_sqlite_members = []

    # Section 20: checked BEFORE zipfile even opens the archive -- a
    # structurally-valid ZIP with unaccounted trailing bytes (or a
    # comment) is invalid under Lumina's own v1 format contract
    # regardless of whether zipfile itself would happily parse it.
    raw_bytes = _raw_bytes_for_trailing_check(archive_source)
    if raw_bytes is not None:
        trailing_error = _check_no_trailing_zip_bytes(raw_bytes)
        if trailing_error:
            errors.append(trailing_error)

    try:
        zf = zipfile.ZipFile(archive_source, "r")
    except (zipfile.BadZipFile, OSError) as e:
        return {"valid": False, "errors": errors + [f"not a valid zip archive: {e}"], "manifest": None,
                "verified_sqlite_members": []}

    with zf:
        try:
            raw_names = zf.namelist()
        except Exception as e:
            return {"valid": False, "errors": [f"could not read archive member names: {e}"],
                    "manifest": None, "verified_sqlite_members": []}

        if len(raw_names) != len(set(raw_names)):
            seen = set()
            dupes = {n for n in raw_names if n in seen or seen.add(n)}
            errors.append(f"archive contains duplicate zip member name(s): {sorted(dupes)}")

        # infolist is central-directory metadata only -- no member is
        # decompressed to obtain it -- so it is read unconditionally,
        # independent of whether raw_bytes (needed only for the
        # physical-structure/trailing-byte checks below) is available.
        try:
            infolist = zf.infolist()
        except Exception as e:
            return {"valid": False, "errors": errors + [f"could not read archive central directory: {e}"],
                    "manifest": None, "verified_sqlite_members": []}

        # AGENT-BACKUP-RESTORE-A2F10 (HIGH H2, Section 29-31), repaired by
        # AGENT-BACKUP-RESTORE-A2F11 (P0, Section 11-13): a
        # decompression/verification resource budget, enforced BEFORE
        # zf.testzip() or any zf.read() below can materialize arbitrary
        # decompressed bytes -- A2R10 measured a 72,530-byte archive
        # decompressing a single 64 MiB member (~925:1) adding ~60 MiB
        # RSS during ordinary receipt verification, with no ceiling at
        # all. Uses ONLY trusted, un-decompressed ZIP central-directory
        # metadata (info.file_size -- the format's own authoritative
        # declared uncompressed size, required to build a valid archive
        # at all) -- never a hostile manifest's own self-reported "size"
        # field (Section 30) -- checked both per-member and in aggregate
        # (Section 31: absolute limits, not a compression-ratio
        # heuristic, so legitimate highly-compressible state such as a
        # zero-filled or repetitive-text member is never wrongly
        # rejected purely for compressing well).
        #
        # AGENT-BACKUP-RESTORE-A2R11 (P0 BLOCKER): A2F10 deliberately used
        # _resolve_configured_payload_budget() here -- the raw OPERATOR-
        # configured ceiling, up to 16 GiB via environment override, with
        # NO host-memory clamping at all. That reasoning ("build-time
        # charging is already clamped downward from this same ceiling, so
        # verify-time can just reuse it directly") only ever covered a
        # build's OWN self-verification of the archive it JUST produced
        # -- it never covered verifying an EXISTING/PRIOR archive found on
        # disk, standalone verify_agent_backup()/build_backup_receipt()
        # calls, or recovery-artifact verification, none of which were
        # ever charged against ANY build's budget in the first place. A
        # 256 MiB host with a tiny-compressed/large-uncompressed prior
        # archive could still decompress it in full during ordinary
        # replacement-backup verification, unbounded by host safety,
        # merely because the (default 2 GiB, operator-raisable to 16 GiB)
        # configured ceiling alone authorized it. A2F11 closes this by
        # deriving the SAME host-safe working-set contract's own
        # verification-phase limit here instead
        # (_determine_verification_payload_budget) -- resident_bytes_hint
        # plus this archive's own already-resident raw bytes, plus a
        # bounded per-member structural allowance for the declared member
        # count, are all reserved from the SAME working-set ceiling the
        # build side derives from, so no verification anywhere in this
        # module can ever decompress past what the current host can
        # safely support, regardless of how high the operator has raised
        # the configured ceiling.
        already_resident = resident_bytes_hint + (len(raw_bytes) if raw_bytes is not None else 0)
        decompression_budget = _determine_verification_payload_budget(
            resident_bytes_hint=already_resident, member_count=len(infolist))
        budget_errors = []
        aggregate_declared_uncompressed = 0
        for info in infolist:
            if info.file_size > decompression_budget:
                budget_errors.append(
                    f"member {info.filename!r} declares an uncompressed size of "
                    f"{info.file_size} byte(s) in the central directory, exceeding this "
                    f"module's single-member decompression budget of "
                    f"{decompression_budget} byte(s) -- refusing to "
                    f"decompress it"
                )
            aggregate_declared_uncompressed += info.file_size
        if aggregate_declared_uncompressed > decompression_budget:
            budget_errors.append(
                f"archive members declare {aggregate_declared_uncompressed} aggregate "
                f"uncompressed byte(s) in the central directory, exceeding this module's "
                f"aggregate decompression budget of {decompression_budget} "
                f"byte(s) -- refusing to decompress any of it"
            )
        if budget_errors:
            # Reject before EITHER zf.testzip() or any zf.read() below
            # runs -- both would decompress every member's full content.
            return {"valid": False, "errors": errors + budget_errors, "manifest": None,
                    "verified_sqlite_members": []}

        # Section 19-26 (A2R8/A2F8, BLOCKER B4): independently validate the
        # archive's own PHYSICAL byte layout -- zipfile's already-decoded
        # member list alone cannot reveal a leading prefix or a
        # concatenated archive, both of which zipfile transparently
        # tolerates (see _validate_zip_physical_structure's own module-
        # level docstring for the full mechanism).
        if raw_bytes is not None:
            errors.extend(_validate_zip_physical_structure(raw_bytes, infolist))

        try:
            bad_entry = zf.testzip()
        except Exception as e:
            return {"valid": False, "errors": errors + [f"archive failed integrity self-test: {e}"],
                    "manifest": None, "verified_sqlite_members": []}
        if bad_entry is not None:
            errors.append(f"corrupt archive member (CRC mismatch): {bad_entry}")

        try:
            raw_manifest_bytes = zf.read("manifest.json")
        except KeyError:
            return {"valid": False, "errors": errors + ["manifest.json missing from archive"],
                    "manifest": None, "verified_sqlite_members": []}
        except Exception as e:
            return {"valid": False, "errors": errors + [f"could not read manifest.json: {e}"],
                    "manifest": None, "verified_sqlite_members": []}

        try:
            manifest = _strict_json_loads(raw_manifest_bytes)
        except Exception as e:
            # Deliberately broad: malformed UTF-8 in manifest.json raises
            # UnicodeDecodeError, not JSONDecodeError -- both, and anything
            # else json.loads might raise on hostile bytes, must come back
            # as a structured invalid result, never an uncaught exception
            # (Section 9's explicit "malformed UTF-8 returns invalid, never
            # raises").
            return {"valid": False, "errors": errors + [f"manifest.json is not valid JSON: {e}"],
                    "manifest": None, "verified_sqlite_members": []}

        errors.extend(_validate_manifest_schema(manifest))

        # Namespace allowlist/collision rules apply to every PHYSICAL zip
        # entry too, not just declared members -- an undeclared forbidden
        # payload is already caught below by the declared/physical set
        # difference, but applying the same rules directly here gives a
        # clearer error and catches a namespace violation even if it
        # happens to also be declared.
        try:
            physical_as_members = [{"archive_path": n, "logical_id": f"<physical:{n}>"}
                                    for n in raw_names if n != "manifest.json"]
            errors.extend(_check_path_and_identity_rules(physical_as_members))
        except Exception as e:
            errors.append(f"could not validate physical archive namespace: {e}")

        members = manifest.get("members") if isinstance(manifest, dict) else None
        if isinstance(members, list):
            names = set(raw_names) - {"manifest.json"}
            declared = set()
            for m in members:
                if not isinstance(m, dict):
                    continue
                archive_path_m = m.get("archive_path")
                if not isinstance(archive_path_m, str):
                    continue
                declared.add(archive_path_m)
                if archive_path_m not in names:
                    errors.append(f"required member missing from archive: {archive_path_m}" if m.get("required")
                                   else f"declared member missing from archive: {archive_path_m}")
                    continue
                try:
                    data = zf.read(archive_path_m)
                except Exception as e:
                    errors.append(f"could not read {archive_path_m}: {e}")
                    continue
                if isinstance(m.get("size"), int) and len(data) != m["size"]:
                    errors.append(
                        f"size mismatch for {archive_path_m}: manifest says {m['size']}, "
                        f"archive has {len(data)}"
                    )
                actual_hash = hashlib.sha256(data).hexdigest()
                if actual_hash != m.get("sha256"):
                    errors.append(
                        f"hash mismatch for {archive_path_m}: manifest says "
                        f"{m.get('sha256')}, archive contains {actual_hash}"
                    )
                elif m.get("content_kind") == "sqlite_database":
                    # Independently prove the payload is a valid SQLite
                    # database from the ACTUAL archived bytes -- never
                    # trust the manifest's own integrity_check claim
                    # (Section 4). Only meaningful once hash/size already
                    # matched -- otherwise this would just be re-deriving
                    # facts about bytes we already know are wrong. Also
                    # proves the payload matches its DECLARED role's own
                    # schema fingerprint for the two known canonical
                    # database logical_ids, not merely "some valid SQLite
                    # file" (Section 4).
                    role = _SQLITE_ROLE_BY_LOGICAL_ID.get(m.get("logical_id"))
                    ok, reason = _verify_sqlite_payload(data, role=role)
                    if not ok:
                        errors.append(f"SQLite payload validation failed for {archive_path_m}: {reason}")
                    else:
                        verified_sqlite_members.append(archive_path_m)
                elif m.get("logical_id") == "state.preferences.prefs_json":
                    # Section 4: hash/size-correct bytes are not the same
                    # claim as "this is a genuine preferences document" --
                    # AGENT-BACKUP-RESTORE-A2R3 proved prefs.json = the
                    # literal bytes "not-json" still verified valid=True.
                    ok, reason = _verify_prefs_payload(data)
                    if not ok:
                        errors.append(f"preferences payload validation failed for {archive_path_m}: {reason}")

            undeclared = names - declared
            if undeclared:
                errors.append(f"archive contains undeclared physical member(s): {sorted(undeclared)}")

    # Section 14: structured evidence the receipt should derive counts
    # from, rather than re-deriving trust from raw manifest arrays after
    # the fact -- validated_member_count/validated_regenerate_count only
    # count entries that are actually well-formed objects (a malformed
    # entry anywhere already makes the whole archive valid=False via
    # _validate_manifest_schema's array-type checks above, so on a
    # passing report these equal the full counts; on a failing report
    # build_backup_receipt never uses them at all).
    raw_members = manifest.get("members") if isinstance(manifest, dict) else None
    validated_member_count = sum(1 for m in raw_members if isinstance(m, dict)) \
        if isinstance(raw_members, list) else 0
    raw_regenerate = manifest.get("regenerate") if isinstance(manifest, dict) else None
    validated_regenerate_count = sum(1 for r in raw_regenerate if isinstance(r, dict)) \
        if isinstance(raw_regenerate, list) else 0

    return {"valid": len(errors) == 0, "errors": errors, "manifest": manifest,
            "verified_sqlite_members": verified_sqlite_members,
            "validated_member_count": validated_member_count,
            "validated_regenerate_count": validated_regenerate_count}


# ---------------------------------------------------------------------
# Backup receipt (Section 12) -- every claim derived from verified
# evidence, never asserted from intent or from unverified path prefixes.
# ---------------------------------------------------------------------

def _read_archive_bound_once(archive_path: str) -> tuple:
    """Opens archive_path exactly ONCE and returns (data: bytes, size:
    int) both derived from that single os.open() call -- AGENT-BACKUP-
    RESTORE-A2R4 (Section 16/17, R4-B5): the previous build_backup_
    receipt called verify_agent_backup(archive_path) (which opens the
    path itself, internally, then closes it) and THEN separately called
    _sha256_file(archive_path)/os.path.getsize(archive_path), each
    reopening the path BY NAME. A2R4 exploited the gap between those
    opens: verify a genuinely valid archive, replace the pathname with a
    different file before the hash/size calls run, and the receipt comes
    back PASS with hash/size describing the REPLACEMENT, not what was
    actually verified.

    The fix is the same fd-binding technique _connect_sqlite_with_bound_
    identity and _open_verified already use elsewhere in this module for
    the identical class of problem: os.open() binds a descriptor to
    whatever inode exists the instant it succeeds, permanently, immune to
    any later rename/replace of the pathname used to open it. Reading the
    full byte string through that one bound descriptor -- and handing
    those SAME bytes to both the verifier (via io.BytesIO, see
    build_backup_receipt) and the hash/size computation below -- means
    verify, hash, and size all describe one, single, immutable capture of
    the archive, taken at one instant, with no reopen-by-name anywhere in
    the sequence."""
    fd = os.open(archive_path, os.O_RDONLY)
    with os.fdopen(fd, "rb") as f:
        data = f.read()
    return data, len(data)


class _BuildSecurityEvidence:
    """AGENT-BACKUP-RESTORE-A2R5 (Section 12/13/14, BLOCKER): A2F4's fix
    for R4-B4 (build-time evidence vs archive self-assertion) was itself
    forgeable -- build_backup_receipt(archive_path,
    build_credentials_boundary_verified=True) is a PUBLIC function with
    an ordinary boolean keyword parameter, so ANY caller, not just
    build_agent_backup_with_receipt, could mint the claim directly
    against a hand-built archive that never had a single real boundary
    check run against it. Proven live during the A2R5 review.

    This class removes that public parameter shape entirely. Only
    build_agent_backup_with_receipt (below) may construct an instance --
    enforced by requiring a module-private sentinel object
    (_EVIDENCE_SENTINEL) that is never exported, never a documented
    parameter, and never reachable by passing a boolean to anything.
    This is NOT a cryptographic barrier: nothing in a shared Python
    process is truly unforgeable against other code with equal
    privilege in the SAME process (Section 14 is explicit about this).
    The real, narrower goal it does achieve: an archive can never
    self-assert this claim (it is never read from manifest/archive
    bytes at all); no PUBLIC caller can accidentally or carelessly mint
    it by passing a boolean; and future ordinary application code has no
    documented, discoverable way to enable it short of reaching into
    this module's own private internals on purpose.

    Section 15: evidence must describe the archive it was built FOR, not
    be reusable across a different one -- archive_sha256 binds this
    instance to the exact bytes build_agent_backup_with_receipt observed
    immediately after its own build_agent_backup call succeeded. The
    receipt constructor below only trusts the evidence if that hash
    matches the bytes it is independently, separately re-reading and
    re-hashing right now -- a mismatch (reused evidence, a since-modified
    archive) silently falls back to the honest 'not provable' answer
    rather than trusting stale or foreign evidence."""
    __slots__ = ("archive_path", "archive_sha256")

    def __init__(self, sentinel, archive_path: str, archive_sha256: str):
        if sentinel is not _EVIDENCE_SENTINEL:
            raise AgentBackupError(
                "_BuildSecurityEvidence may only be constructed by "
                "build_agent_backup_with_receipt, immediately after its own "
                "build_agent_backup call has actually succeeded"
            )
        self.archive_path = archive_path
        self.archive_sha256 = archive_sha256


_EVIDENCE_SENTINEL = object()


def build_agent_backup_with_receipt(data_dir: str, base_dir: str, dest_path: str, **kwargs) -> tuple:
    """The ONLY code path that may honestly produce a receipt with
    build_credentials_boundary_verified=True (Section 13). Calls
    _build_agent_backup_core directly (not the public build_agent_backup)
    and builds evidence from its returned _BuiltArchiveResult's already-
    realized `.built_data`/`.built_sha256` -- plain, immutable Python
    values computed EAGERLY inside that call, immediately after its own
    verification succeeded, never a file descriptor or a path read at
    any later moment (Section 10-14).

    AGENT-BACKUP-RESTORE-A2R6 (Section 15-18) closed the PATH-level
    replacement gap: the prior design called build_agent_backup (whose
    own build-time evidence was already discarded) and then separately
    reopened dest_path BY NAME afterward, letting a replacement archive
    slip in during that gap. AGENT-BACKUP-RESTORE-A2R7 (Section 10-14,
    BLOCKER) found A2F6's own fix -- deferring the actual byte-read to a
    file descriptor opened early but read late -- was still incomplete:
    a same-inode IN-PLACE rewrite (no new inode, no path replacement at
    all) landing in that deferred window would have been read as if it
    were the verified build. There is no reopen-by-name AND no deferred
    descriptor-read left anywhere in this call chain now: the evidence
    this function uses was already fully realized as bytes before
    _build_agent_backup_core even attempted to publish anything.

    The returned receipt additionally carries a `publication` key
    (Section 16) -- the SEPARATE, honestly time-bounded observation that
    dest_path was re-read and found to match the built bytes at a
    specific moment during the build. This is evidence about what was
    OBSERVED at that moment, never a promise that dest_path remains that
    way afterward (Section 11: not achievable against an arbitrary
    concurrent writer, and not claimed). Returns (manifest, receipt)."""
    result = _build_agent_backup_core(data_dir, base_dir, dest_path, **kwargs)
    evidence = _BuildSecurityEvidence(_EVIDENCE_SENTINEL, result.dest_path, result.built_sha256)
    receipt = _build_receipt_from_bytes(result.dest_path, result.built_data, result.built_size, evidence)
    receipt["publication"] = result.publication_observation
    return result.manifest, receipt


def build_backup_receipt(archive_path: str) -> dict:
    """A structured, machine-derived success receipt computed from the
    verified archive/manifest -- every field here is measured, never
    asserted from intent. Deliberately says
    archive_policy_credentials_excluded (derived from the verified
    manifest's own exclusion record, never hardcoded), never the stronger
    and untestable claim that the archive 'contains no secrets' -- Lumina
    cannot detect a token a human pasted into an ordinary text file she is
    designed to archive verbatim.

    This PUBLIC function can never produce build_credentials_boundary_
    verified=True (AGENT-BACKUP-RESTORE-A2R5, Section 12, BLOCKER: it
    used to accept a caller-supplied boolean for exactly this claim,
    which any code could set regardless of whether it built anything).
    Only build_agent_backup_with_receipt, which never reaches this
    function at all, may assert that claim -- see _BuildSecurityEvidence."""
    try:
        data, size = _read_archive_bound_once(archive_path)
    except OSError as e:
        return {"destination": archive_path, "verification": "FAIL",
                "errors": [f"could not open archive for verification: {e}"]}
    return _build_receipt_from_bytes(archive_path, data, size, evidence=None)


def _build_receipt_from_bytes(archive_path: str, data: bytes, size: int,
                               evidence: "_BuildSecurityEvidence | None") -> dict:
    """Shared receipt construction from an already-read, already-bound
    byte capture of the archive (Section 16/17, R4-B5: verify, hash, and
    size all describe this SAME `data`, never a path reopened by name).
    `evidence`, when given, is trusted ONLY if it is bound (by exact
    archive_path AND archive_sha256) to precisely these bytes -- see
    _BuildSecurityEvidence's own docstring for why a mismatch must fall
    back to 'not provable' rather than ever being trusted regardless.

    Field semantics, stated precisely (AGENT-BACKUP-RESTORE-A2R7, Section
    15): `archive_sha256`/`archive_size`/every other field this function
    returns describe `data` -- the EXACT bytes passed to this call,
    nothing else. For `build_backup_receipt(archive_path)` (an arbitrary,
    possibly pre-existing archive), that means 'the archive as observed
    at the moment this call read it.' For `build_agent_backup_with_
    receipt` (Section 10-14), `data` is `_BuiltArchiveResult.built_data`
    -- bytes realized EAGERLY, at build time, before publication -- so
    these fields describe the IMMUTABLE built artifact permanently, by
    construction, regardless of anything that happens to the mutable
    `archive_path`/dest_path afterward. Neither this function nor its
    caller ever implies that `archive_path` is guaranteed to still
    contain these exact bytes at any later moment: that is a promise
    about a mutable, user-owned filesystem path that cannot be made
    against an arbitrary concurrent writer (Section 11), and this module
    does not make it. A caller that needs to know whether a path
    CURRENTLY holds these bytes must check again, at that time -- see
    build_agent_backup_with_receipt's own `publication` key for the one
    such check this module performs, itself explicitly timestamped and
    scoped rather than treated as a standing guarantee."""
    try:
        report = _verify_agent_backup_inner(io.BytesIO(data))
    except Exception as e:
        # Mirrors verify_agent_backup's own "never raises" belt-and-
        # suspenders wrapper -- this function bypasses that public
        # wrapper (it needs the archive_source-accepting inner call
        # directly, so it can reuse the already-read bytes rather than
        # reopening archive_path by name) and must therefore replicate
        # the same guarantee itself.
        return {"destination": archive_path, "verification": "FAIL",
                "errors": [f"unexpected error verifying archive: {e}"]}

    if not report["valid"] or report["manifest"] is None:
        return {"destination": archive_path, "verification": "FAIL", "errors": report["errors"]}

    manifest = report["manifest"]
    members = manifest.get("members", [])
    verified_sqlite = set(report.get("verified_sqlite_members", []))

    def _count_prefix(prefix):
        return sum(1 for m in members if isinstance(m, dict) and
                   isinstance(m.get("archive_path"), str) and m["archive_path"].startswith(prefix))

    project_names = set()
    for m in members:
        ap = m.get("archive_path", "") if isinstance(m, dict) else ""
        parts = ap.split("/")
        if len(parts) >= 3 and parts[0] == "overlay" and parts[1] == "projects" and parts[2] != "projectlist.md":
            project_names.add(parts[2])

    exclusions = manifest.get("exclusions", [])
    credentials_excluded = any(
        isinstance(e, dict)
        and e.get("logical_id") == _CANONICAL_CREDENTIALS_EXCLUSION["logical_id"]
        and e.get("reason") == _CANONICAL_CREDENTIALS_EXCLUSION["reason"]
        for e in exclusions
    )

    archive_sha256 = hashlib.sha256(data).hexdigest()
    build_verified = "not_provable_from_archive_alone"
    if evidence is not None and evidence.archive_path == archive_path \
            and evidence.archive_sha256 == archive_sha256:
        build_verified = True

    return {
        "destination": archive_path,
        "archive_sha256": archive_sha256,
        "archive_size": size,
        "format_version": manifest.get("format_version"),
        # Section 14: derived from the verifier's own structured evidence
        # (validated_member_count/validated_regenerate_count), never a
        # raw len() of the manifest's own arrays -- on a passing report
        # these are equal (a malformed entry anywhere already fails
        # verification above), but the receipt reads the verifier's
        # count, not the manifest's own claim, on principle.
        "member_count": report.get("validated_member_count", len(members)),
        "database_snapshots_verified": len(verified_sqlite),
        "overlay_count": _count_prefix("overlay/"),
        "projects_count": len(project_names),
        "skills_count": _count_prefix("overlay/skills/"),
        "personas_count": _count_prefix("overlay/personas/"),
        "flight_recorder_included": "state/telemetry/flight_recorder.db" in verified_sqlite,
        "archive_policy_credentials_excluded": credentials_excluded,
        "build_credentials_boundary_verified": build_verified,
        "regenerate_count": report.get("validated_regenerate_count", len(manifest.get("regenerate", []))),
        "rebind_required_count": sum(
            1 for m in members if isinstance(m, dict) and m.get("portable") == "PORTABLE_WITH_REBIND"
        ),
        "warnings": manifest.get("warnings", []),
        "verification": "PASS",
    }
