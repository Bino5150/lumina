# Attribution

This page is the canonical correction record for co-author attribution in
this repository's commit history.

It exists because GitHub resolves `Co-authored-by:` trailers to real
accounts. A malformed address does not simply fail to be credited — it can
credit the *wrong person*. That happened here once, and this document is the
correction.

---

## Third-party misattribution (the one that matters)

**Commit:** `86a5f728e3ed0bf6560b8c0d2ff622038f5c0540`
**Subject:** `fix: surface refused fixed-provider endpoints`
**Date:** 2026-09-21

**Published trailer:**

```
Co-authored-by: Lumina <lumina@users.noreply.github.com>
```

That address resolves to an unrelated GitHub account named `lumina` (all
lowercase). It is not this project's agent account and had no involvement
in this work. The account was credited as a co-author of this repository in
error, on this one commit only — it is the only commit in the repository's
history carrying that address.

**Intended Lumina identity:**

```
Co-authored-by: Lumina <therealagentlumina@gmail.com>
```

Lumina's GitHub account is **`Lumina5150`**. Its canonical commit identity
is the address above. Note the deliberate distinction: `Lumina5150` is the
GitHub login; `therealagentlumina@gmail.com` is the address used in commit
trailers.

**Why this commit was not rewritten:** the published commit is preserved
exactly as released. Rewriting it would invalidate established public
ancestry for every clone, fork, and downstream reference — damage
substantially greater than the malformed trailer it would remove. This
page is the correction of record.

---

## Malformed internal trailers (same commit, different class)

The same commit also carries two non-canonical internal trailers:

```
Co-authored-by: Claude  <noreply@anthropic.com>
Co-authored-by: ChatGPT <noreply@openai.com>
```

These are a **different defect class** from the entry above. They are not a
third-party misattribution: both addresses belong to the vendors those seats
actually use. The defect is that the seat names are non-canonical — this
project's standing identities are `Claude Sonnet 5` and `Sol 5.6`, and an
unnamed `Claude` or `ChatGPT` trailer is not one of them.

**Intended form:**

```
Co-authored-by: Claude Sonnet 5 <noreply@anthropic.com>
Co-authored-by: Sol 5.6 <noreply@openai.com>
```

---

## Other historical anomalies (informational)

These are recorded for completeness. None affects a current contributor, and
none is rewritten.

**Uppercase/word-boundary corruption.** An earlier memory-compression defect
in this project's own tooling abbreviated identity fields, producing
trailers such as `LUM <therealagentLUM@gmail.com>` (commits `109a741e6`,
`b3d0e9c4a`). Root cause was fixed in `tools/palace.py`; these are its
historical residue in published commits.

**Cross-domain address swaps.** Several commits carry an OpenAI-family seat
name with an `@anthropic.com` address, or vice versa — for example
`Codex GPT-6 <noreply@anthropic.com>`, `Sol <noreply@anthropic.com>`, and
`Lumina <noreply@anthropic.com>`. Where a seat address was confirmed wrong,
the *next* commit carries the corrected form (see the seat law below).

---

## Seat law: contributor identities are stable seats

Contributor identity does not track the underlying model. When a seat's
model changes, the attribution identity does not change with it.

The canonical seat identities for this repository:

| Seat | Trailer |
|---|---|
| Bino | `Bino5150` (GitHub) / `bino5150@gmail.com` |
| Lumina | `Lumina <therealagentlumina@gmail.com>` |
| Claude Sonnet 5 | `Claude Sonnet 5 <noreply@anthropic.com>` |
| Sol 5.6 | `Sol 5.6 <noreply@openai.com>` |
| Codex | `Codex <noreply@openai.com>` |

So the trailer is `Codex <noreply@openai.com>` — **not** `Codex GPT-6`, not
`Codex GPT-6.1`, and not any future model label. Seats are stable;
implementations are not.

This rule was adopted after commit `ab9c80b1`, a metadata-only amend that
corrected the 01D-B candidate's `Codex GPT-6 <noreply@anthropic.com>`
trailer to `Codex <noreply@openai.com>`. The amend changed commit metadata
only: the tree, parent, and every tracked file were byte-identical before
and after.

---

## Reporting a further attribution problem

If you believe a commit credits you or someone else incorrectly, open an
issue with the commit SHA and the address in question. Attribution errors
are treated as corrections, not as dismissals — but they are corrected
here in documentation rather than by rewriting published history.