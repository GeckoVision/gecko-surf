# Architecture decision records

An ADR here records a decision about **how the engine is built**: a seam, an
invariant, a default, a contract between modules. One file per decision, named
`YYYY-MM-DD-<slug>.md`, never edited after it is accepted — a decision that turns
out wrong gets a NEW record that supersedes it, because the reasoning that was
wrong is the part worth keeping.

## Why this directory exists next to `docs/decisions/`

`docs/decisions/` is **gitignored**, and so is the root `CLAUDE.md`. That is correct
for what lives there: business model, pricing, ICP. It was not correct for
everything, and on 2026-09-25 we found out how. Four decisions made that day —
admitting a fourth adapter seam, moving a build default off a partner's endpoint,
deferring a deny list to another repository's contract, and never moving a release
tag — were recorded in a gitignored instructions file, two PR bodies and a code
comment. Not one of them shipped with the code they govern.

A PR body is not a decision record. It is findable only by someone who already
knows which PR to open.

So: **engineering decisions here, in the open, tracked. Business strategy stays in
`private/` and `docs/decisions/`.** If a record would embarrass us in a public repo,
it is strategy and it is in the wrong folder.

## What belongs in one

A record is worth writing when the decision is **hard to reverse**, **crosses a
module contract**, or **had a real alternative that someone will propose again**.
If none of those is true, a code comment is the right size.

Each one states: what forced the decision, what was decided, what the option not
taken costs, how reversible it is, and what is now forbidden. The last line matters
most — an ADR that does not forbid anything did not decide anything.

## Records

| | |
|---|---|
| [2026-09-25](2026-09-25-buildcall-fourth-adapter-seam.md) | `BuildCall` is the fourth adapter seam, and the build default moves in-engine |
| [2026-09-26](2026-09-26-windowless-hosts-sign-in-the-users-wallet.md) | **Proposed.** On a surface with no signing window, the user's own wallet asks us for the transaction (Solana Pay). Custody paths refused or gated |
