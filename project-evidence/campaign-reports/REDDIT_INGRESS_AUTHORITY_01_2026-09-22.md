# REDDIT-INGRESS-AUTHORITY-01 — Source Vet, Adversarial Receipts, and Repair

Date: 2026-09-22

Release baseline: `86a5f728e3ed0bf6560b8c0d2ff622038f5c0540` (`main`)

Dev baseline: `eaf66d2ab002dabae5bd693d7722c4414c198f87` (`dev-local`)
Disposition: repaired shared provenance defects; Reddit end-to-end acceptance unavailable because no Reddit transport exists in the release tree

## Repository gate

- `/home/bino/lumina-release` was clean at the baseline above.
- `/home/bino/lumina` was already dirty and remains untouched. Its pre-existing residue included modified `personas/jarvis.json` and `projects/lumina-dev/codebase.md` plus unrelated untracked files/directories.
- `dev-local` is a descendant of release baseline `86a5f72`.
- Dev push is disabled (`release-local` push URL `DISABLED`, `push.default=nothing`).
- Public release push remains owner-operated. No push or dev mirror was performed by this campaign.

## Current ingress map — verified from live source

There is no Reddit bridge, Reddit OAuth/authentication, Reddit inbox/comment retrieval, Reddit normalization, Reddit reply/post path, or Reddit transcript persistence implementation in the release tree or its reachable Git history.

The current transport boundary is:

- Local GUI and CLI construct owner agents.
- Telegram accepts only the configured numeric chat ID before invoking an owner agent.
- Discord hardcodes `owner=False`, a fixed non-owner tools profile, and per-channel cached agents.
- `core/headless.py` converts non-owner ingress to `EXTERNAL_CHANNEL_INBOUND`.
- `core/context.py` frames external and tool content as lower-trust user/tool-role data and maintains a sticky session reminder.
- `tools/memory.py` persists chat row metadata; `core/context_reconstruction.py` restores external user-row source metadata.
- Dream, manual compaction, and automatic trim compaction write model-authored summaries to Palace.
- Palace is passively injected into the owner SYSTEM prompt; per-segment `untrusted` rendering is therefore load-bearing.
- Continuity/Reforge uses a provenance-separated compiler and re-enters as an assistant-role continuity message with a non-authority preamble, not interpolated SYSTEM content.
- Owner-only tools are stripped from non-owner agents independently of profile/PIN selection.

## Confirmed defects

### RIA-01 — second-order synthesis laundering

Before repair, Dream and manual compaction decided whether a model-authored summary was lower-trust by scanning only the immediate source slice for an `EXTERNAL_CHANNEL_INBOUND` user row. This failed when:

1. Reddit-like external proposition `P` entered as external.
2. Lumina paraphrased `P`.
3. The original external row aged out of Dream's newest-40 window, or had already been covered by an earlier incremental manual-compaction checkpoint.
4. A later summary saw Lumina's paraphrase but not the original external row.
5. The summary was stored with `untrusted=False` and passively re-entered owner SYSTEM context without a lower-trust bracket.

Pre-repair targeted result: the new campaign file produced three intended failures; the Dream and manual-compaction cases were two of them.

Repair: every Dream, manual-compaction, and automatic-compaction summary is now durably lower-trust. This preserves the information and normal Palace resurfacing while denying model authorship the power to reset provenance.

### RIA-02 — My Human paraphrase laundering

Before repair, owner-profile curation removed the external user row but retained assistant rows. A Lumina paraphrase of an external claim therefore entered `human_profile_curated`, which `core/context.py` explicitly renders as authoritative.

Pre-repair targeted result: the captured curation input contained `assistant: REDDIT_POISON...` even though the source external row had been filtered.

Repair: authoritative My Human curation now accepts only direct owner `user` rows. It excludes external rows, assistant prose, and mixed `parts_v1` dropped-file rows. Legacy user rows without source metadata retain the established owner fallback.

### RIA-03 — cached-headless caller authority mismatch

Before repair, cached-agent tool gating correctly used immutable `agent.owner`, but `run_headless_turn()` stamped source from the later call-time `owner` argument. A mismatched `owner=True` cache hit on a genuine non-owner agent could therefore label the turn `OWNER_DIRECT` even though capabilities remained non-owner.

Pre-repair targeted result: the campaign regression expected `EXTERNAL_CHANNEL_INBOUND` and received `OWNER_DIRECT`.

Repair:

- Headless source assignment and response sanitization now use the cached agent's immutable owner state.
- `LuminaAgent.chat()` independently clamps every real `owner=False` agent to `EXTERNAL_CHANNEL_INBOUND`, even if a direct caller requests `OWNER_DIRECT`.
- Callers may lower trust on an owner agent; they cannot raise trust on a non-owner agent.

### RIA-04 — legacy synthesized Palace rows

Read-only live SQLite inspection (`PRAGMA quick_check = ok`) found:

- 1,898 chat rows.
- 0 chat rows stamped `EXTERNAL_CHANNEL_INBOUND`.
- 325 Palace drawers; 323 already lower-trust.
- 75 Dream drawers; 73 lower-trust.
- Two historical Dream drawers still trusted: IDs 294 and 313 in `nightstand/143`.
- No automatic- or manual-compaction drawers in the live database.
- A non-empty curated human profile (1,602 characters); content was not printed or modified.

Repair: `init_palace_db()` now performs an idempotent one-way migration for historical `dream-sweep`, `auto-compaction`, and `manual-compaction` drawers. It flips matching trusted drawers lower-trust, adds the observability tag, and rebuilds affected rolling closets from drawer authority bits so already-compressed segments are also bracketed. The migration was verified against isolated SQLite state and was not executed against the live owner database during this campaign.

## Regression coverage

`tests/test_reddit_ingress_authority_01.py` covers:

- Direct fake SYSTEM/owner/delegation/quotation/obfuscation specimens remain external user-role data.
- An authenticated-looking `u/Bino5150` identity remains non-owner because transport source is external.
- Dream second-order paraphrase persistence remains lower-trust after the original external row leaves the window.
- Incremental manual compaction cannot promote a Lumina paraphrase.
- Assistant prose cannot enter authoritative My Human curation.
- Historical synthesized drawers and closets migrate lower-trust idempotently.
- Normal external conversation remains readable and useful.
- Cached non-owner authority cannot be promoted by a later caller argument.
- A direct caller cannot stamp a non-owner agent as owner or restore owner-only tools.
- The equivalent local owner turn remains structurally distinct and untagged.

`tests/test_compaction_trigger.py` additionally freezes automatic compaction's always-lower-trust write contract.

## Causality evidence

- Before the synthesis/profile repair: `5 collected`, `2 passed`, `3 failed` (Dream, manual compaction, My Human).
- After that repair: `5 passed`.
- Before the cached-headless repair: the targeted cache-mismatch regression failed with actual `OWNER_DIRECT` vs expected `EXTERNAL_CHANNEL_INBOUND`.
- After that repair and the direct-agent clamp: the campaign file passed.
- The startup migration test constructs a trusted legacy Dream row, runs initialization twice, and verifies the drawer bit/tag, closet bracket, and sticky aggregate are repaired without a second-pass change.

## Verification

- Initial relevant baseline: `156 passed`, native exit 0.
- Expanded persistence/context focused gate: `258 passed`, native exit 0.
- Agent/headless focused gate: `201 passed`, native exit 0.
- Palace/migration focused gate: `98 passed`, native exit 0.
- Final campaign file: `9 passed`, native exit 0.
- Full release suite: `4,879 passed`, `38 deselected`, `1 warning`, native exit 0 (`525.39s`).

## Remaining architectural limitation

The current product structurally gates capabilities by agent/channel ownership and tool profile. It does not carry a full proposition-level dependency graph or a machine-verifiable causal link from every future mutating tool call back to a specific owner authorization event.

This repair closes observed authority promotion across ingress stamping and all current model-authored durable summary writers. It does not claim that the existing boolean/sticky provenance representation can prove arbitrary semantic derivation across unlimited paraphrase chains. Building per-claim provenance or universal intent-bound authorization would be a larger architecture project and was not introduced speculatively.

## Acceptance status

Verified in the current release substrate:

- External source identity and familiar names do not promote channel authority.
- Model-authored Dream/compaction summaries cannot become owner-authoritative Palace segments.
- My Human cannot absorb external claims through assistant paraphrase.
- Legacy synthesized summaries migrate in the safer direction.
- Non-owner agents cannot be caller-promoted to owner provenance or owner-only tools.
- Useful external conversation remains available.

Not claimable in this campaign:

- Real Reddit authentication/account recognition.
- Real Reddit inbox/comment/thread retrieval.
- Real Reddit outbound replies.
- A live Bino-on-Reddit vs GUI/Telegram comparison.
- Production Reddit restart/rehydration acceptance.

Those require a real Reddit transport, which does not exist in the source vetted here. The mandatory real-Bino-on-Reddit acceptance case is preserved as a future transport acceptance gate, not falsely reported as passed.

## Release actions

- Local release checkpoint: this report's containing commit (created only after exact-final-tree verification).
- Public push: **NOT PUSHED**.
- Release-to-dev mirror: **NOT PERFORMED**.
- Live owner data: **NOT MUTATED**.
