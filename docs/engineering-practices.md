# Engineering practices

Lightweight organization for how we ship. **No ceremonies, no estimation, no RFC
processes, nothing that requires a meeting.** Everything here is an artifact you read
in the PR flow.

## 1. The delivery blurb — five lines before a line of code

At the top of every spec (and echoed in the PR body):

```
WHY:    the user pain, one sentence (with the evidence link)
WHAT:   the deliverable, one sentence
PROOF:  the falsifiable check that says it works (the a-ha number)
GAP:    what this deliberately does NOT do (the honest boundary)
DOCS:   which living docs this changes (architecture / llms.txt / README / CLAUDE.md) — or "none"
```

The `PROOF` line front-loads Pattern B thinking (what offline check falsifies this?).
The `DOCS` line is what keeps the architecture docs alive — doc drift becomes visible
at PR time.

## 2. ADRs — one page per one-way door

Irreversible decisions get a file in `docs/decisions/`: context → decision →
consequences. One page. Examples of ADR-worthy calls: a compose boundary, a
source-trust policy, an evidence gate, a public tool shape. Reversible choices don't
need one.

## 3. Definition of Done — the PR checklist

Every PR:

- [ ] `ruff format` · `ruff check` · `mypy gecko` clean
- [ ] Targeted tests named and passing (never a bare full-sweep dispatched to an agent)
- [ ] Honesty labels present (recorded vs live; fork vs mainnet; measured vs estimated)
- [ ] Security gate consulted if the change touches an invariant or a detection rule
- [ ] Docs: `[ ] no claim moved` `[ ] claim moved → updated in this PR`
- [ ] Memory/decision captured if a strategy call was made

**A claim moves** iff the PR changes: a public tool/CLI shape, a boundary or invariant
statement, anything crossing the WORKS ↔ NOT-built line, or a headline number.
Flipping a NOT-built bullet is part of the shipping feature's Definition of Done.
**Non-triggers** (most PRs): refactors, renames, internal moves, test-only changes,
bugfixes that move no claim.

## 4. Docs cannot rot silently

- `tests/test_docs_claims.py` asserts the mechanical truths: files/links referenced by
  the README and `architecture.llms.txt` exist, and module paths named in the docs are
  real. CI fails on a dead link or a renamed module.
- At each release tag, one ~20-minute pass: regenerate diagrams and counts, reread the
  NOT-built list against the tree, refresh the Status paragraph in `CLAUDE.md`.
  Numbers live in exactly one home; other docs link, never restate.

## 5. Sprint OKRs — measured, never aspirational

Written at sprint start, graded in place at close. No separate artifact, no review
meeting.

- **Objective** = the sprint's JTBD sentence, user-visible.
- **KRs** = 2–4 numbers the toolchain already emits: differential counts, receipt
  catches (naive-fails → gecko-passes pairs), FCC %, compression %, confirmed drift
  events, grep-verifiable zeros. Every KR falsifiable by one command. No task-%
  completion, no vanity metrics (downloads ≠ usage). If the number doesn't exist yet,
  the KR is "the measurement exists and reads X."

Worked example (the catalog-graph sprint):

> **Objective:** An agent pointed at the Orquestra catalog finds the right starting
> instruction and gets a provenance-tagged plan that survives simulation — without
> hand-authored configs.
> - KR1 — auto-comprehend differential: 4/4 programs ≡ hand-authored, 0 diffs outside
>   the declared overlay. **Met.**
> - KR2 — receipt catches: 2 live proof-pairs on a $0 fork (pump ❌3012→✅86,669 CU;
>   Meteora ✅81,964 CU). **Met.**
> - KR3 — find_start honesty floor: 0 fabricated starts; every miss logged
>   categorically. **Met.**
> - KR4 — provenance: 0 ladder Literals outside `gecko/provenance.py`. **Met.**

## 6. Standing rules (unchanged, load-bearing)

- Pattern B: offline-falsifiable first; live smoke is the final check, never the
  debugger.
- Bug fixes start with a failing test.
- Doing-not-having copy; a fork result is never labeled mainnet; FLAGGED survives
  every merge.
- Code-writing agents run sequentially; never `git checkout` while one runs.
- Confirm before anything outward or destructive.
