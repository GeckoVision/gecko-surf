# Delivery report — the consolidated plan, executed

**Date:** 2026-08-12 · **Against:** [`docs/specs/2026-08-12-consolidated-plan.md`](./2026-08-12-consolidated-plan.md)
**Scope:** Wave 0 (PR triage) through Wave 5. Every wave in the plan is closed.

This document exists so the delivery can be *evaluated*, not admired. It states what
changed, why, what proves it, what I got wrong, and what is still open. Where a claim is
measured, the measurement is here. Where it is not, it says so.

---

## 1. The one-paragraph version

Seven PRs merged, two closed as refuted, one opened and green. The work fell into three
kinds: **repairs to guards that could not fail** (four tests that would have stayed green
through the bug they existed to catch), **measurements that did not exist** (retrieval
floors, a wrong-instruction metric, an idempotency proof), and **one new capability** (a
fallback token-leg basis, which changed shape twice under review because the safe-sounding
version authorized a drain). The single most important outcome is not a feature: it is that
three separate things we believed were protecting us were measured and found not to be.

---

## 2. What shipped

| PR | State | What it is | Size |
|---|---|---|---|
| [#396](https://github.com/GeckoVision/gecko-surf/pull/396) | merged | `let_me_buy` — the two gaps the live probe proved (S4 remainder, rebased) | 5 files, +1055 |
| [#397](https://github.com/GeckoVision/gecko-surf/pull/397) | merged | The consolidated plan itself, split out of #395, both decisions ruled, four claims corrected | 2 files, +570 |
| [#398](https://github.com/GeckoVision/gecko-surf/pull/398) | merged | **U2** — a guess is not a retrieval; check each point's own kind | 2 files, +147 |
| [#400](https://github.com/GeckoVision/gecko-surf/pull/400) | merged | **Wave 2, G1–G4** — four guards that could not fail, each proven by its own mutation | 3 files, +126/−17 |
| [#401](https://github.com/GeckoVision/gecko-surf/pull/401) | merged | **Wave 4** — the retrieval floors main never had, the direction-inverse guard, a metric that had to be built | 3 files, +360 |
| [#402](https://github.com/GeckoVision/gecko-surf/pull/402) | merged | **N2** — the `os-keychain` backend, so the declared default exists | 2 files, +781 |
| [#403](https://github.com/GeckoVision/gecko-surf/pull/403) | merged | **B3** — the reservation is idempotent on exact bytes | 6 files, +594/−71 |
| [#404](https://github.com/GeckoVision/gecko-surf/pull/404) | **open, green** | **F1** — the instruction-trace token leg | 3 files, +1390 |
| [#385](https://github.com/GeckoVision/gecko-surf/pull/385) | closed | Half refuted: store-name provenance honest, self-transfer guard not wired | — |
| [#386](https://github.com/GeckoVision/gecko-surf/pull/386) | closed | Refuted on floor semantics (see §5) | — |

Two PRs predating this work remain open and were deliberately not touched:
[#395](https://github.com/GeckoVision/gecko-surf/pull/395) (the Privy signing half) and
[#399](https://github.com/GeckoVision/gecko-surf/pull/399) (a demo PR).

---

## 3. How to check this yourself

```bash
uv run ruff format && uv run ruff check --fix
uv run mypy gecko                       # 175 source files, clean
uv run pytest -q                        # 3,527 passed, 63 skipped, 2 xfailed
```

Five failures are expected in a network-restricted shell and reproduce on untouched `main`:
three in `test_connect.py` and one in `test_privy_auth.py` fail on DNS
(`could not resolve host: auth.privy.io`), and `test_pda_testkit.py::test_surfpool_fork_reaps_child_process`
fails on a sandboxed child process. None are ours.

The parts worth reading directly:

```bash
uv run pytest tests/test_token_leg_instruction_basis.py -q   # 42 — F1
uv run pytest tests/test_os_keychain_backend.py -q           # 24 — N2
uv run pytest tests/test_spend_policy.py -q                  # 63 — B3 + G3/G4
uv run pytest tests/test_retrieval_eval.py -q                # 36 — Wave 4
```

---

## 4. Wave 0 — PR triage

**The problem.** Four open PRs, two stalled, and — the actual root cause named in the plan —
**two disjoint plans**. The spec's PR-1…PR-11 and `STATE.md`'s NEXT list shared exactly one
item, so every dependency graph either of us drew was computed over half the work.

**What I did and why.**

- **#396 merged.** Already complete; it was waiting on nothing.
- **#385 closed.** Superseded by #396, and its blocking rationale had gone stale. Closed
  with the reasoning written into the PR rather than dropped silently — half of it was
  honest work (store-name provenance) and that half should be findable later.
- **#386 closed** under your ruling D-A. See §5; this one is the most interesting close.
- **#395 split.** It carried two unrelated things: the plan document and a Privy signing
  implementation. The document was the thing unblocking every other decision, and it was
  sitting behind review of a signing change. I extracted it, corrected four claims in it,
  and merged it as #397. The signing half stays open under the gating conditions now
  recorded in the plan.

**Why splitting mattered more than it looks.** The plan was the merge of the two disjoint
plans. Leaving it inside a signing PR meant the artifact that fixes "we are running two
plans" was itself blocked by one of the plans.

---

## 5. The two decisions you ruled, and what they cost

Recorded in §5 of the plan. Both were surfaced rather than assumed, because both had
consequences I could not reverse alone.

**D-A — close #386.** The Lucene-lens retrieval change measured **+0.0303 recall@1**, which
sounds like a straightforward improvement. It is not: it changes *what the floor refuses at
limit=5*. The benefit was real but essentially zero at the margin, and it preserved a
regression in what the router declines to serve. Closing it kept a precision property we
would have traded away for a rounding error in recall.

**D-B — permit a one-way digest into the ledger.** This unblocked B3. The spend ledger now
stores a hash derived from transaction bytes, which is a narrow, deliberate concession
against invariant #1 (control plane, never data plane). It is bounded three ways: the digest
is one-way, it expires after `DEDUPE_SECONDS` (15 minutes), and the amount outlives the
digest so the daily cap keeps working after the row stops saying *which* transaction it was.
A test pins each bound.

---

## 6. Wave 1 — U2: a guess is not a retrieval

**The problem.** The retrieval evaluator counted a *guess* as a hit. Rows the router
answered without actually retrieving the right kind of point inflated every published
number.

**What I did.** Rebased the existing fix onto main, checked each point's own kind, and
**restated every published number**: MRR moved **0.8409 → 0.8333**. The falsifier turns red
on main's bytes, which is what makes the correction a measurement rather than an assertion.

**Why it matters for evaluation:** every retrieval figure quoted before this is 0.0076 too
high on MRR. The corrected numbers are the ones in §8.

---

## 7. Wave 2 — four guards that could not fail

This is the wave I would look at hardest, because all four defects share a shape: **a test
that passes whether or not the property it names holds.** Each is now proven by a named
mutation going red.

| | The guard | What it missed | The mutation that proves it now |
|---|---|---|---|
| **G1** | `sign()` takes no parameter through which a verdict could arrive | It read only the top level of an annotation, so `SpendVerdict \| None` and `list[SpendVerdict] \| None` walked straight through | Add either parameter → red |
| **G2** | No sign/broadcast path in the preflight runner | It scanned identifiers, not **string literals** — `_METHOD = "sendTransaction"` was invisible | Add the literal → red |
| **G3** | The velocity counter is labelled advisory | It asserted on the word "advisory", which also appears in the module overview — the entire RESIDUALS section could be deleted with the suite green (measured: 51 passed) | Delete the paragraph → red |
| **G4** | The gate reads no verification field off the receipt | It did not allowlist which amount fields may be read, so a **second amount source** could be introduced | Read `tokens_received` → red |

**Why G3 is the one to notice.** It is the failure mode where a test enforces a *label*
instead of a *claim*. I hit the same class again in F1 and pre-empted it there (§10).

---

## 8. Wave 4 — the floors main never had

**The problem.** Retrieval had no regression tripwire at all, and the floors drafted on the
abandoned branch **could not have worked**: they were written as `0.7576` and `0.9091`,
rounded *up* from 25/33 and 30/33 and compared with `>=`. They would have failed at exact
parity with the very measurement they were derived from.

**What I did.**

- Floors as **exact fractions**, which removes the defect class rather than the instance:
  `recall@1 ≥ 25/33`, `recall@3 ≥ 30/33`, `MRR ≥ 5/6`, `false_accepts ≤ 8`, over
  `scoreable == 33`. A test asserts each floor *equals* its source measurement, catching a
  rounded literal at authorship.
- **R-1, the direction-inverse guard**, corrected in scope: the requirement is not that an
  inverse never appears, but that **the gold outranks its inverse**. Proven non-vacuous by a
  constructed counterexample that fires when the order flips.
- **R-2, `wrong_instruction_accepts`** — this did not exist and had to be *built* before it
  could be treated as a gate. It is read at **`SERVE_LIMIT = 5`**, the depth production
  actually serves, not at `EVAL_LIMIT = 10`. A test pins the divergence: the router refuses
  at 5 what it serves at 10.
- **R-4** rescoped: the CLI exit code is the two-sided falsifier, and it was confirmed to
  operate at production depth.

**The honest caveat.** `FALSE_ACCEPT_CEILING = 8` is a ceiling, not an achievement. Those 8
are authored — out-of-scope rows carrying an identity term — and driving that number *down*
is the improvement. It is pinned so it cannot grow unnoticed.

---

## 9. Wave 5 — the signing lane

**N2 — the declared default did not exist.** `DEFAULT_SIGNER_PROFILE_NAME` has been
`"os-keychain"` since the signer seam was written, described there as the local-first
default. **No backend implemented it**, so the documented-secure default refused every
transaction, and the only two concrete backends were the *weaker* profiles — a plaintext
keypair file and an external custody provider.

That is not a safety hole; a signer with no backend refuses, and refusal is the correct
direction. It is the product gap that *becomes* a safety hole by social pressure: the secure
default did not work, so the path of least resistance was to name `developer-keypair-file`
and put a key on disk. **A default that refuses teaches its users to reach past it.**

Design choices worth reviewing:
- It lives in `scripts/`, because `SigningBackend`'s own contract says "implemented outside
  `gecko/`, always". Holding key material for another party is what custody *is*, so the
  package boundary is a one-way door.
- The secret is **not held on the instance**. The pubkey is kept; the material is re-fetched
  per signature and discarded in a `finally`. A heap dump between signatures has no key, and
  revocation is immediate — a test removes the key after `open()` and asserts the refusal.
- Resolution is **pinned to one backend**, never chained. The credential chain (keyring →
  command → env) is right for an API token, where a miss costs a 401. For a signing key it
  would mean a locked keychain silently promotes an environment variable to the thing that
  signs.

**B3 — idempotent reservation.** A retry of the *same bytes* used to reserve budget twice.
It now dedupes on an `exact` message binding. Two properties are load-bearing and both are
pinned: two **distinct** transactions are never collapsed (collapsing them would be a cap
bypass reachable by repetition), and the key is the **exact** binding, not the structural
one — structural normalises the blockhash, so two deliberate identical transfers would
collapse into one reservation.

**The residual is named, not hidden:** a retry that had to refresh its blockhash is
different bytes and does reserve again. That is kept deliberately rather than closed with a
blockhash-insensitive digest, which would reintroduce the collapse above.

**A side effect worth flagging.** Three existing cap tests were passing for the wrong
reason. They meant "N transactions" and said it by calling `_tx()` N times — which only
worked while the gate could not tell repetition from distinctness. B3 exposed that; they now
build N genuinely distinct transactions.

---

## 10. Wave 3 — F1, the instruction-trace token leg (open as #404)

The largest and most-revised unit. It changed shape twice, both times because the
safe-sounding version was not safe.

### 10.1 The scoping call refuted the plan's safety argument

The plan justified F1 as safe because it is fallback-only and can therefore "only move
refuse → (authorize | refuse)". That is true, and it **quietly includes authorizing an
unbounded drain**.

The balance arrays state, per token *account*, the **owner** and the balance change. An
instruction states the **authority** that signed it. Under SPL delegation those differ —
`approve` lets a delegate move tokens out of an account it does not own. So the natural
implementation filters out movements the payer did not authorize, and:

> a filtered movement sums to zero → a summed zero is an **observed zero** → an observed
> zero passes every cap.

Reachable without a second signer: the payer signs `approve(delegate = PDA_X)` once in an
earlier transaction; today's transaction is one allowlisted top-level call, and program X
CPIs `transferChecked` out of the payer's ATA under `PDA_X`. The transfer exists only in
`innerInstructions`.

**Resolution:** a non-payer authority **refuses** (`authority-not-payer`) rather than
filtering. The report fails closed as a *whole*, like the arrays path — a clean movement
beside a refused one does not rescue it, because nothing proves the refused movement is not
the drain.

### 10.2 Two design calls you made

- **Shape:** the `instruction-trace` basis carries `instruction_outflows` directly.
  `TokenMovement` is balances-shaped and needs pre/post, which instructions do not have.
- **Scope:** only the `*Checked` variants are priceable; everything else refuses, and the
  narrowed recovery is stated honestly rather than papered over. An unchecked `transfer`
  states no mint and no decimals, and the array production decoders recover them from is
  precisely what is absent here.

### 10.3 The falsifier runs where it has to hold

A parser that refuses correctly beside a signing path that authorizes anyway is exactly the
failure this repo keeps naming ("wired ≠ reaches the agent"). So the falsifier runs
`simulate()` → `Receipt` → the **real** `SpendPolicyGate`, with a policy that says yes to
every other predicate. The drain comes out `amount-unresolvable`.

**Its counterexample sits beside it** — same wiring, same policy, one field different, a
real 25 USDC outflow priced and charged. Without it, the test above would pass on a gate
that refuses everything. F1 **recovered a measurement**; it did not just add a way to say no.

### 10.4 The security review (constraint 6) found four holes

Each was reproduced first-party before it was fixed, then re-measured after.

| # | Hole | Measured before the fix |
|---|---|---|
| 1 | **A node could choose which basis measured it.** `parse_token_deltas` checks null *first* and returns, so one nulled array suppressed every stronger refusal | `preTokenBalances: null` beside a `postTokenBalances` showing the payer's Token-2022 account falling 100 USDC → 0 downgraded a hard `token-2022-extensions-unread` refusal into an **authorized 1 USDC spend** |
| 2 | **Token-2022 priced on no extension evidence** — `mint_extensions` was never plumbed into the fallback, silently repealing "unread is not none" for every mint this basis sees | A T22 `burnChecked` priced at 25 USDC with zero evidence |
| 3 | **The mint was node-authored and uncorroborated** | Naming an allowlisted, generously capped mint while moving a different one turned `mint-not-allowlisted` into an authorized spend |
| 4 | **Two node-triggered faults that were not refusals** | `"²".isdigit()` is `True` and `int("²")` raises → a `ValueError` escaping `simulate()`; unbounded `decimals` through `10 ** decimals` spent 0.116s building a 1 MB string at 10⁶ (the review also measured 4.8s at 10⁷, which I did not re-run). Both now bounded by `_MAX_DECIMALS = 18`, as the arrays path already was |

Hole 1 is the one I would want a second opinion on, because the fix is a *narrowing*: the
fallback now triggers on the absent key only. It costs nothing today — stock
`simulateTransaction` omits the keys, it does not null them — but it is a behavioural
narrowing justified by a claim about what real nodes do.

**One design decision inside the fix.** `account_keys` is **required, with no default**. An
absent extension set means *refuse*, so its default is the safe direction; an absent account
set would mean *skip the check*. That asymmetry is what decides whether a default may exist.

### 10.5 What F1 honestly cannot do

Written into the docstring and **pinned by a test** — G3's lesson applied pre-emptively,
since a residual nobody pins is a residual somebody deletes:

- **The amount stays node-authored.** The mint is corroborated and the authority checked;
  the quantity cannot be, because it lives in argument bytes `DecodedInstruction` discards by
  design. This basis bounds *which* mint and *whose* authority, never *how much*.
- **An omitted CPI reads as an observed zero.**
- **A zero here is weaker than a zero from the arrays** — it means "no priceable,
  payer-authorised CPI named this mint", not "the balance was observed not to change".
- **Only `*Checked` is priceable**, which is why this basis is safe more often than useful.
- **Pairing the receipt to the right bytes is the binding check's job**, not this one's.

---

## 11. What went wrong

Stated because a delivery you cannot audit is not a delivery.

1. **I lost work to a `git checkout`.** Reverting a mutation with
   `git checkout HEAD -- gecko/simulate.py` also discarded the uncommitted F1d wiring. I
   re-applied and re-verified it, but it cost a cycle. **The rule I should have been
   following, and now do: commit before mutating.** The two later mutation proofs were run
   against committed bytes.

2. **A vacuous test I wrote myself.** The F1 mutation M2 ("pretend an unchecked transfer is
   priceable") passed *vacuously*, because the test omitted the mint and amount and so
   refused on missing fields rather than on the allowlist. Rewritten to supply every field
   and still expect refusal — which pins allowlist membership rather than field presence.
   This is the same defect class as G1–G4, produced by me, in the same session I spent
   fixing four instances of it.

3. **A 1.2 MB video nearly reached `main`.** A pathless `git add` in the B3 work swept four
   unrelated files into #403 — a workflow note, an adoption-metrics spec, and
   `demo/kit/gecko_101.mp4` with its thumbnail. Caught before merge. Removed from the index
   only, inside the same branch, so the squash merge landed a clean net diff and no video
   blob reached main's history; the files remain on disk, untracked, exactly as they started.
   Verified after merge.

4. **B3 was reported as merged when it was not.** It was open the whole time, and F1 was
   silently stacked on it. Caught when the F1 diff showed spend-policy files it had no
   business touching.

---

## 12. What is not done

- **#404 (F1) is open and green.** Not merged — awaiting your review, since it is the unit
  whose design changed most.
- **#395 (Privy signing)** remains open under the gating conditions in the plan.
- **#399 (demo)** is green and untouched by this work.
- **Message-hash binding on the signing gate is still not built.** The gate is verdict-based.
  Unchanged by this delivery, and worth restating so the status section stays honest.
- **The V2 feedback-capture design decision is still open.** Nothing here resolves whether
  Gecko sees call outcomes without breaking invariant #1.
- **Willingness-to-pay is still unvalidated.** No part of this delivery touches the thesis
  decider. Everything above is engine quality.

---

## 13. Numbers, restated in one place

| Metric | Value | Note |
|---|---|---|
| Full suite | 3,527 passed, 63 skipped, 2 xfailed | + 5 sandbox-only failures that reproduce on `main` |
| `mypy gecko` | clean, 175 source files | |
| recall@1 floor | 25/33 ≈ 0.7576 | exact fraction, `>=`, measured at `15b5044` |
| recall@3 floor | 30/33 ≈ 0.9091 | |
| MRR floor | 5/6 ≈ 0.8333 | **corrected down from 0.8409 by U2** |
| false_accepts ceiling | 8 | authored; driving it down is the improvement |
| wrong_instruction_accepts | 0 at `SERVE_LIMIT = 5` | metric built this session |
| Dedupe window | 900s | after which the digest expires and the amount outlives it |
