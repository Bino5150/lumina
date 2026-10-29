# Agent Backup and Incomplete State Export

Settings → Memory provides three separate workflows:

| Workflow | What the result means |
|---|---|
| **Backup Memory (Legacy)** | Existing data-directory ZIP, with its existing exclusion checks. Source overlays are outside its scope. It is useful for bounded future salvage, not a complete Agent Backup. |
| **Verified Agent Backup** | Checks eligibility for a complete backup. Currently refuses: no approved C1 safety-state snapshot contract exists. It writes no archive and never silently falls back. |
| **Incomplete State Export** | Local owner explicitly acknowledges missing coverage, then chooses a **new** file. Structure, hashes and SQLite snapshots are verified. Coverage remains incomplete and full recovery acceptance remains unproven. |

Exports run locally, without a model tool or cloud upload. The new exporter runs
in a worker thread retained by the application. Closing Settings does not cancel
an active export; normal application shutdown waits for it to finish. Only one
new export runs at a time across Settings instances. Canceling the acknowledgement or save dialog does not collect
or publish anything. The acknowledgement defaults to **No**. Existing destinations,
including previously verified backups, cannot be overwritten by Incomplete State
Export. A concurrent destination creation also blocks publication atomically.

No Restore control is provided in Settings, and no restore into a live or
production data folder exists. An engineering-only isolated restore engine can
restore an archive into a new, empty, non-production data folder for testing; it
is described in [the recovery contract](agent-backup-recovery-contract.md). It
is not production disaster-recovery activation. Because no archive captures a
C1 safety-state snapshot, it never produces a Verified Agent Backup: verified
archives restore as Incomplete State Export, damaged ones only as owner-selected
Selective Recovery, and Backup Memory (Legacy) ZIPs as limited Legacy Recovery.
Optional encrypted credentials recovery is a separate future campaign.

## Coverage and authority

The exporter reuses the A2 collectors with explicit data and source roots. It
captures immutable bytes, snapshots SQLite with its online backup API, verifies
the finished archive, publishes those exact bytes and reobserves the output
before reporting a measured receipt. Required capture/security/integrity failures
block the export; they cannot turn into a partial-success fallback.

| Durable family | Incomplete export policy |
|---|---|
| Shared SQLite database | Chats, raw messages, flat memories, Palace, Knowledge Base, Skills metadata and other tables present in that database survive in the snapshot. This is structural preservation, not a cross-store recovery acceptance. |
| Flight Recorder | Separately verified SQLite snapshot; historical telemetry. |
| Preferences | Reviewed portable content only: human bios/curated notes, persona reference, selected voice/display settings, and `disabled_tools`. Unknown keys are omitted and counted as unclassified coverage gaps. |
| PIN hash/salt, keys, owner/channel bindings, endpoints and Telegram master state | Omitted from portable prefs. Exported Telegram remote access is explicitly false. Destination PIN/owner binding and security restrictions must be preserved/reconciled locally before any future activation. Missing archive fields must never clear destination restrictions. |
| Personas, Skills Markdown, avatars, voices, tool profiles | Source overlays collected separately from the data directory. Hash-based baseline classification remains informational provenance, never a grant. |
| Projects | Source project list/docs/codebase index plus data-root bindings and chat links. Machine paths require destination-local rebind. |
| Custom tools, pending actions and audit | Historical content only. Imported executable approvals/grants remain inactive and tools require quarantine/reapproval. |
| Knowledge Pack installation marker | Required v2 control member wraps validated `knowledge_packs_installed.json` records. Missing source is encoded as `missing_unknown` with `records: null`, never first-run proof or a fabricated installation. Future Restore must reconcile it before bootstrap to avoid resurrecting deliberately deleted entries. Malformed or unsafe present markers block export. |
| C1 suspension store, soul identity, evidence, incidents, epochs, replay state | Unavailable and explicitly omitted. Enforcement compatibility is unproven. No identity is manufactured, store initialized, epoch reset, or restriction removed. An initialized store, including an active suspension, permits historical export through the C1-owned exclusion interface. Protected paths and observed inodes are refused by every collector without opening their contents. Absence remains unknown. |
| Optional operator identity trailers and baseline metadata | Captured if safely present; neither is credentials or authority. Absent baseline evidence leaves provenance unknown. |
| Credentials/auth sessions, idempotency ledger, worktrees, caches and live execution | Excluded. The ledger would be regenerated empty by a separately approved future recovery engine. |

Restrictions archived as historical content cannot authorize runtime action. A
future recovery engine must apply destination security supremacy, union/reconcile
restrictions rather than erase them, keep communications off until owner-local
rebind, and independently establish C1 enforcement compatibility. This candidate
does not implement those activation decisions.

## Format and receipt

Incomplete State Export writes explicit **major 2, minor 1**, with
`backup_mode: incomplete_state_export`. Its mandatory recovery and coverage
contracts distinguish structural integrity, semantic coverage, C1 coverage and
enforcement compatibility, and full recovery acceptance. The latter is always
`not_proven`, with `restore_input_accepted: false`. “PASS” applies only to archive
structure, member hashes and SQLite validity. It never means complete continuity
or verified restorable.

New exports embed the exact C1-owned `c1_exclusion` record: schema/version,
`status` (`absent_unknown` or `present_excluded`), observation time and warnings,
`snapshot_captured: false`, `enforcement_compatibility: unproven`, unknown C1
schema/suspension status, and `restore_fence: c1_trace_and_owner_recover_required`.
The exporter describes exclusions before any source capture and rechecks the
stable boundary immediately before atomic publication. An unsafe or changed
boundary refuses publication and discards its temporary file, preserving prior
backups. Ordinary SQLite sidecar churn does not block export. No protected paths,
inode identities, counts or selector contents are placed in this record.
Earlier 2.0/2.1 historical archives without the record remain readable with their
original incomplete coverage; missing records do not establish clearance.

The v2 reader also accepts the supported 2.0 core contract. Version 2.1 adds a
deliberate history extension mechanism at manifest and member level:
`extensions: {"version": 1, "records": {"example.history": {"version": 1,
"data": {}}}}`. Namespaces contain at least two dot-separated lowercase names;
each segment starts with a letter and may contain digits, underscores and hyphens.
Unknown records are preserved as opaque historical data. They cannot override
core policy or supply current authority. Each envelope is limited to 256 KiB,
64 namespaces, depth 16 and 10,000 JSON values; keys are at most 256 characters
and namespaces at most 128. Unversioned extra core/member fields are refused.
In 2.1, `regenerate[]` records require string `logical_id`, `state_class` and
`restore_policy`; `exclusions[]` records require string `logical_id` and `reason`.
Both allow an optional string `note` and the same bounded `extensions` envelope.
Other direct fields are refused. Historical metadata, including authority-looking
claims, belongs in opaque extensions and never becomes current control authority;
no namespace grants special trust. Canonical policy checks still apply. These
record checks leave v1 behavior and supported 2.0 historical compatibility intact.
Unknown `required_features` (at most 64 distinct names) preserve structurally
valid history but require a future compatible handler before activation. They
cannot waive the known C1 fence. Unsupported physical member families still need
an explicit future format contract; extensions do not bypass ZIP member rules.

Knowledge Pack markers preserve bounded future pack identities, unknown fields
and deletion records, without granting official-package trust. Marker history is
limited to 1 MiB, depth 16 and 10,000 JSON values. Known version/time fields retain
their types. Missing marker data remains unknown. These bounds are deliberate
compatibility limits: an out-of-policy marker blocks export rather than silently
discarding records. Unknown preference keys are still omitted and counted; a
future custody policy is needed before exporting them, because they may hold
credentials or machine authority.

The mandatory `historical_claims` policy distinguishes builder observations made
during capture from archive-declared historical claims and independently
authenticated provenance. Archive-contained hashes prove internal consistency,
not historical authenticity. An explicitly owner-designated surviving archive
may supply historical truth to a fresh installation without prior destination
records. That designation does not authorize execution, official-package trust,
owner promotion, recovered approvals, credentials or C1 clearance.

The public A2 v1 builder/verifier retain their default contract. The v1 verifier
rejects v2; the v2 verifier rejects v1, legacy and incompatible policies. Shared
base-member validation is reused internally without changing the accepted v1
schema. Old v1 archives are classified as older coverage: they did not capture
the installation marker or C1 contract and copied prefs under a different policy.
Neither an absent manifest field nor a version rewrite can upgrade their coverage.
Legacy ZIPs are classified from bounded metadata before any payload read. A
legacy database is accepted only when its archive holds no pending WAL or hot
journal; a WAL-mode header alone is normal for a live Lumina database. No
legacy import into a live data folder is provided; the isolated engineering
restore can recover a legacy ZIP into a new folder as limited Legacy Recovery.

Classification is an explicitly unverified format hint, including when an archive
declares a known version. Both classifier and v2 verifier use the existing A2
host/configured verification resource budget before decompression; there is no
separate 4 MiB manifest cutoff. The classifier conservatively reserves space for
two archive copies. A legal large manifest still needs enough host memory.

The displayed receipt includes the observed SHA-256, verified member and database
counts, missing/unknown coverage, C1 compatibility, and recovery acceptance.
Directory fsync success is measured separately from content verification. A failed
fsync is reported as unconfirmed durability; a failed write, unsafe capture or
verification cannot receive a success receipt. If interference occurs after
publication, the workflow reports failure and leaves evidence for owner review;
it does not delete or overwrite an unowned replacement.

The receipt's `restore_input_accepted: false` records that full recovery acceptance
has not been demonstrated by this exporter. It is not an eternal veto on a future
approved partial or historical recovery workflow. The acceptance requirements,
including owner-local emergency recovery, are described in
[the recovery contract](agent-backup-recovery-contract.md).

**Archives are not encrypted.** Credential stores are excluded by policy, but
ordinary chat, memory, source content or user text may contain secrets. An export
is not permission to publish or upload its contents.
