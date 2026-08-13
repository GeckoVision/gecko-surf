# The one plan — everything pending, merged

**2026-08-12.** Supersedes `2026-08-12-autonomous-signing-blockers.md`, which contains a
false premise (§2.1) and schedules two items that are already done.

**Read §2 before scheduling anything.** The structural reason work has felt circular is
that we were running **two disjoint plans** — the spec's PR-1…PR-11 and STATE.md's NEXT
list share exactly one item. Any dependency graph computed over either was computed over
half the work. This file is the merge.

Base: `origin/main = d55ff8c` (#393). Every claim below was verified against that SHA with
`git show`/`git diff`, cold bytecode cache, not against a working tree and not against
STATE.md.

**Revised 2026-08-12 (later).** §5 records both founder rulings and the Wave-0 PR triage
outcome. Four claims in §6 were re-measured first-hand and **corrected in place** — Wave 1's
"ordering is a choice", R-2's limit premise, R-3's scope, and R-4's line number — each marked
**CORRECTION** where it stands. Rule 3 of §7 applies to this document too: the corrections
below were measured, not relayed.

---

## 1. What is PROVEN — do not re-litigate

| Claim | Evidence |
|---|---|
| The full loop runs autonomously on mainnet | tx #12, one call, no human in the loop |
| CU prediction is exact | 12 of 12 mainnet transactions |
| Gecko can hold **no key at all** | Privy enclave; `scripts/privy_backend.py` holds only an app secret |
| The program-id allowlist exists on **both** sides of the seam | ours (`allowed_instructions`) + Privy policy rule `jee7y79fr8e5rm520b1xe8bt` |
| S2's two defects are closed on main | `.isdecimal()` at `simulate.py:414-425`; decimals refusal at `:764-801`, probe-proven |
| S3's spend precondition is real | `signer.py:334` field, `:336-342` signature, `:493-501` refusal, `:449-453` asked over re-verified bytes |
| The SOL leg works on a fork today | `sol_delta = -5000`, matches mainnet exactly |

---

## 2. Corrections to the record — things we believed that are FALSE

### 2.1 The trust-root premise I wrote (most consequential)

I asserted a `getAccountInfo`-derived token leg *"reinstates the RPC as a trust root, which
`gecko/txbind.py:285-289` and `gecko/spend_policy.py:1031-1037` already refuse."* **False.**

Those sites govern the **SUBJECT** — what the transaction *is* (programs, account keys,
selectors, ALT resolution). They never govern the **AMOUNT**. And the SOL leg **already is**
a `getAccountInfo` differential inside the signing path on main: `simulate.py:1089` (pre),
`:1106-1107` (post via the `accounts` field), `:1150-1154` (`sol_delta = post - pre`),
reached from `autonomous_purchase.py:388-397`.

The codebase's real line is: **SUBJECT comes from signed bytes only; EFFECT is necessarily
node-derived** — a CPI's effect is not knowable from bytes, which is why we simulate at all.
Acting on my version would have sent us to replace surfpool or abandon a reachable lane.

### 2.2 Other corrections

- **PR #380 does not exist.** `gh pr view 380` → "Could not resolve to a PullRequest". The
  commit subject cites it anyway. Branch `fix/gold-rank-skip-guess` is at `9f2a145` with no PR.
- **R1 `dbf4e69` DID land** — as `e6a6b20` (#383, MERGED, a rebase); all four touched blobs
  byte-identical. Closes R2-C6 with zero work.
- **PR-8 and PR-9 are already done.** Strike them.
- **PR #386's blocking evidence names the wrong row.** The meteora limit=5 flip it cites is a
  **gain** (main refuses row 15 entirely at limit 5; R2 serves the correct gold at rank 2).
  The genuine regression is the two pumpfun *buy* rows.
- **PR #386's "R2 rewrites card text" premise is false** — zero config changes.
- **surfpool cannot be configured to return token balances.** Hardcoded `None` at
  `crates/core/src/rpc/full.rs:2698-2727` across v1.1.1 → v1.5.0 and current main; structural,
  because surfpool is built on LiteSVM whose `TransactionMetadata` has no token-balance fields.
  ~40 CLI options enumerated; no flag exists. **Refuted, not unresearched.**
- **The `balances-null` refusal fires unconditionally on a fork** — even for token-FREE
  transactions, because surfpool nulls `preBalances`/`postBalances`/`fee` too.
- **Two prescribed fixes are not implementable as written.** STATE.md's X2 (raw whole-file
  substring scan) is RED on unmutated main — `fork_preflight.py:15` legitimately names
  `sendTransaction` in a docstring. PR-2's DoD contains a fake edge:
  `scripts/prepare_purchase.py` never signs and never reads a slot.
- **PR-7's DoD is internally unsatisfiable** with the levers that exist: +1 distinguishing term
  is a measured no-op; +2 hits the false-accept target (8/12 → 2/12) but drops recall@1 to
  0.6364 and recall@3 to 0.7576, both below PR-7's own floors.

---

## 3. The root cause

**Zero of the four recent failures are in shipped production controls.** All four are in the
**second-order layer that proves controls correct**: two test guards, one eval metric, one
spec DoD (plus the relay). Production controls that were mutation-tested held.

- *"We design against the shape of data we've seen"* — **unrefuted, but strike "adversary."**
  Three of four involve the codebase's **own idioms**: a `types.UnionType`, a string literal,
  a closed `MissCause` Literal.
- *"Metrics inherit the fix's blind spot"* — unrefuted in its **weak** form only, and it is a
  symptom of the above. The mechanism is the closed `Literal`, not author identity.
- *"Prose specs let false premises survive"* — **REFUTED twice.** The gate caught PR-1 *as
  prose*, the cheapest possible point; and PR-1's own DoD carried the same false premise, so
  writing it as code would have passed. The defect is the **fixture**, not the medium.
- **Evidence transport in the dispatch graph** — real, independent, and now the single most
  reproducible defect we have. **At least eight recurrences.** A ruling node whose only
  evidence path is a relayed table is fail-open by construction.

---

## 4. Signing — the options, settled

Our seam needs two properties: accept **arbitrary serialized bytes**, and return them signed
**without broadcasting**, because we re-bind the signed message against the receipt's attested
binding *before* anything reaches the chain.

| option | raw bytes? | broadcasts? | key lives | autonomous? | verdict |
|---|---|---|---|---|---|
| **Privy server wallets** | yes, base64 | no (`signTransaction`) | enclave | yes | **PROVEN — tx #12, 0.35 s** |
| **Phantom embedded** | no | **forced** | Phantom | browser OAuth | **disqualified** |
| **Phantom injected (extension)** | yes | no | user's browser | no — human | **right for the playground** |
| **1claw Intents** | no (`to`/`value`/`chain`) | optional `sign_only` | HSM | yes | **cannot express our instruction** |
| **Local keypair file** | yes | no | disk | yes | works (`developer-keypair-file`) |
| **OS keychain** | — | — | OS store | yes | **declared default, NO BACKEND EXISTS** |

**Phantom disqualifies itself in its own kit, four times:** *"Use `signAndSendTransaction` —
NOT `signTransaction`. Embedded wallets do not support `signTransaction`"*; and
`transactions.md:144`: *"`signTransaction` is only available with the injected provider."* If
the vendor broadcasts, our re-binding happens after the money moved — a post-mortem, not a
refusal. **It is still the correct integration for the playground**, where a human signs.

**1claw:** 258 paths, zero occurrences of `serialized_transaction`, `raw_transaction`,
`solana_message`, `instructions`, `account_metas`, `unsigned_transaction`. Solana support is
`to` + `value` + optional `token_mint`/`token_decimals`/`memo`. It cannot sign
`let_me_buy::make_purchase`. Ask them whether a raw-Solana-message path is planned — they
already have `xrpl_tx_json` for XRP and `eip712_digest` for EVM.

**Adopt from 1claw regardless of partnership:** server-side guardrail counters
(`tx_spent_today`, `tx_count_today` — ours is advisory and resettable by the process it
bounds); `tx_overhead_budget` + `solana_ata_allowlist` (**non-value drain: rent, fees, ATA
creation — we have no concept of this, and every cap we own reads zero against it**);
`Idempotency-Key` with 24 h TTL.

---

## 5. Decisions only the founder can make — BOTH RULED 2026-08-12

Neither was resolvable by research. Both are now settled; the consequences are folded into §6.

- **D-A — does a gold point served as a `guess` count as a hit? RULED: NO.** A guess is not a
retrieval. This ratifies U2 / `9f2a145` and makes it the first thing to land. Two consequences:
every published recall/MRR number is restated (main MRR 0.8409 → 0.8333, recall@1 unchanged
at 25/33), and **R2's delta is 0.0000, not +0.0303** — which removes R2's entire measured
benefit and leaves only its pumpfun-buy regression. **PR #386 is CLOSED**, not rescheduled.
- **D-B — may a one-way digest of a message binding enter the spend ledger? RULED: YES.** A
one-way digest is *correctness metadata*, not payload — it is not reversible to a
transaction, a response body, or a secret, so invariant #1 holds. This unblocks B3/PR-4:
the idempotency key must be **bound to transaction identity via the digest**, so that one
key reused across different transactions cannot yield free authorizations. That was the
PR-1-shaped hazard, and binding is what closes it.

### Wave-0 outcome (PR triage), recorded 2026-08-12

- **#396 MERGED** as `9b3c507` — S4's remainder, rebased not patched.
- **#385 CLOSED, superseded.** Part (a) is what #396 shipped. Part (b)'s blocker is **stale**:
`check_plan_accounts` is wired at three production sites on `origin/main`
(`gecko/prepare_purchase.py:424`, `gecko/autonomous_purchase.py:362`,
`scripts/prepare_purchase.py:129`).
- **#386 CLOSED** under D-A. Salvaged into Wave 4: R-1, the floors, and R-2.
- **#395 SPLIT.** The two spec docs land on their own; the Privy signing half stays open
behind its own gate. A 520-line coordination artifact must never wait on a signing review.

---

## 6. The work — one merged list

Falsifier rules: a guard repair is proven by its **named mutation going RED**, never by a
green test. Never a bare `uv run pytest`. Every measurement cold
(`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` cleared).

### Wave 1 — U2 alone, first

**U2 · the guess-skip.** Rebase `9f2a145` onto main and open the PR that never existed.
*Every retrieval number we hold is inflated by a guess counted as a retrieval.* **Ratified by
D-A.**
Falsifier: `uv run pytest tests/test_retrieval_eval.py::test_the_bonding_paraphrase_is_a_refusal_not_a_rank_4_retrieval`.
Blast radius: `gecko/retrieval_eval.py`, `tests/test_retrieval_eval.py`. No control, no ranking.
Expect main MRR 0.8409 → 0.8333; recall@1 unchanged at 25/33.

**CORRECTION — "applies cleanly to both main and main+R2, so ordering is a choice, not a
constraint" was FALSE.** `git merge-tree` reports *changed in both* on `gecko/retrieval_eval.py`
(both edit `_gold_rank`) and on `tests/test_retrieval_eval.py` (U2 adds `_gold_rank` unit tests,
R2 adds floor + branch-split tests). Ordering **was** a constraint. It stops being one only
because D-A closed R2 — the conflict is dissolved, not avoided.

### Wave 2 — four guard repairs, in parallel with Wave 1

Test files only. Zero production bytes. All four reproduced first-hand today.

- **G1 (X1) · the union-annotation hole.** `SpendVerdict | None` is a `types.UnionType` and is
  not in the bare tuple at `tests/test_signer_spend_precondition.py:391-393`. Mutation M1b
  passes the whole suite (45 passed, exit 0). Fix: a **recursive** `typing.get_args` walk —
  `get_args` flattens one level only, so `list[SpendVerdict] | None` still passes without
  recursion. Falsifier: **M1b goes RED**.
- **G2 (X2) · string literals invisible to `_code_identifiers`.** Fix with an `ast.Constant`
  scan, **docstrings excluded** — exclusion is load-bearing, `fork_preflight.py:15` legitimately
  names `sendTransaction`. Falsifier: **m11 RED**. File: `tests/test_fork_preflight.py`.
- **G3 (X3) · the residual paragraph is deletable** (51 passed). Falsifier: **m10 RED**.
- **G4 (X4) · `receipt.tokens_received` survives as a second amount source.** Falsifier: **m12 RED**.

G3 and G4 share `tests/test_spend_policy.py` — **serialize those two**. G1 and G2 are disjoint.

### Wave 3 — the fork lane

**F1 · `innerInstructions`-derived token leg.** Measured working on surfpool 1.1.1: the CPI
returns fully `jsonParsed` (mint, raw 100000, decimals 6, authority = fee payer) in the **same
response, same bytes, any stack depth, no second read** — exactly the number mainnet's arrays
give. Not surfpool-specific.

Six non-negotiable constraints:

1. **Strictly a fallback** — the arrays win whenever present. `balances-null` stays a refusal
   when `innerInstructions` is absent. Because it fires only where today's answer is already
   REFUSE, it can only move refuse → (authorize | refuse); it can never lower a currently
   authorized amount.
2. **Provenance-tagged** in the Receipt so a reader can tell which source produced the number.
3. **Closed allowlist that REFUSES on any unrecognised `spl-token`/`spl-token-2022`
   instruction** — summing only `transferChecked` silently misses `transfer`, `burn`,
   `burnChecked`, `closeAccount`/wSOL-unwrap, and T22 withheld-fee withdrawals.
4. **Union of top-level and inner instructions.** `innerInstructions` carries only CPIs, so a
   **top-level** SPL transfer would read as zero. The top-level list is already decoded
   locally at `spend_policy.py:1031`.
5. **`basis` is a closed `Literal` in one module** — `structural` is already taken by
   `BindingStrength` with a different meaning on the same Receipt.
6. **Re-enters a defi-security gate on the DELIVERED BYTES** (40-char SHA + worktree path).

**Escalated, not absorbed — AUTHORITY vs OWNER.** The current path filters on the token
account's **owner**; `innerInstructions` supplies **authority**. Under delegation these
differ, so a delegate moving the payer's tokens *inside this transaction* reads as zero under
an authority filter. The array path catches it; the instruction path does not. This widens an
existing named residual from "a separate transaction" to "inside this one." Owner:
`token-engineer` + `staff-engineer` for the scoping call.

**Do NOT build the `getAccountInfo` token differential** — not for the trust-root reason I
invented (§2.1), but because enumerating *which* token accounts moved needs the static account
keys, which is PR-1's blind spot one layer down: an ATA created **and** drained inside a CPI is
absent from the enumerated set and reads as zero.

### Wave 4 — retrieval (starts today; the spec's edge to PR-6 is FAKE — different files, different controls)

R2 is CLOSED (D-A), so this wave is no longer gated on a merge order. Each item stands alone
on main and is worth landing on its own evidence.

- **R-1 · the direction-inverse regression test.** Forbids a directional inverse (`buy`/`sell`,
  `deposit`/`withdraw`) on the same program being returned as `kind == "start"`. The case is
  **already authored** at `tests/fixtures/find_start_golden.jsonl:10` ("must not land on buy")
  with no assertion reading it — this is a test over a fixture we already own. Assert against
  the gating loop at `gecko/find_start.py:1471-1478`. Confirmed live: `pumpfun/buy` *and*
  `pumpfun/sell` are both `kind == "start"` at limits 5 and 10, so it is RED before the fix.
- **R-2 · `wrong_instruction_accepts` must be BUILT, then measured at limit=5.**
  **CORRECTION — "the regression only appears at limit 5" is FALSE.** Measured on both refs:
  14/34 at limit 5 and 16/34 at limit 10 — *identical on main and R2 at both limits*. The
  metric was blind to R2 **everywhere**, not just at 10. And it is **not a code symbol at
  all** — it existed only in this document, so it could not be "moved"; it has to be written.
  What survives, and is the real finding: the eval measures at `EVAL_LIMIT = 10`
  (`gecko/retrieval_eval.py:64`) while **every production caller serves 5** — `find_start`
  defaults to `limit: int = 5` (`gecko/find_start.py:1368`) and `catalog_surface.py:114`
  passes no limit. Keep `EVAL_LIMIT = 10` for rank resolution (a misrank must stay
  distinguishable from absent); the *floor* metric reads 5.
- **R-3 · the floor block is broken in three places, not one.** `RECALL_AT_1_FLOOR = 0.7576` is
  strictly greater than 25/33 = 0.75757…, so it **fails at exact parity with main**.
  **CORRECTION — `RECALL_AT_3_FLOOR = 0.9091` has the identical defect** (30/33 = 0.90909…),
  and **there is no MRR floor at all.** Both literals are rounded *up* and compared with `>=`.
  Deeper: these floors existed **only on the R2 branch** — `main` has no recall tripwire
  whatsoever, which makes writing them on main the more valuable half of R2. Use exact
  fractions (`25 / 33`), never a rounded decimal literal; that removes the defect class rather
  than the instance.
- **R-4 · re-scope PR-7.** Its DoD is unsatisfiable as written (§2.2). A non-pytest falsifier
  exists and is non-vacuous: `gecko/providers/cli.py:258` (**not :257**) returns 1 when nothing
  clears the floor — `"buy a house"` exits 0, `"flumbuzzle the quantum wombat"` exits 1.
- **R-5 · STRUCK.** It was "R2 merge, only after R-1, R-3 and an integrated cold re-measure."
  D-A closed R2, so there is nothing to merge and no composed tree to re-measure.

**PR-7 gates the autonomous-store claim** and the spec scheduled it fifth, behind four signer
PRs it shares no data with. Move it forward.

### Wave 5 — carried, still unowned

- **A3 · `exact` binding strength** is derived from the caller's own `replace_blockhash` flag
  and never proved against the chain (`simulate.py:388`). No proposal exists.
- **A4 · the single-RPC trust root.** One unauthenticated node chooses the state, the `err`
  and the `unitsConsumed`. Printed on every demo run; unsolved.
- **B2 · FABRICATION residual.** `SignerHandoff` closes omission, not fabrication. Needs a
  construction token minted in `verify_handoff` and verified in `sign`. Flips
  `test_a_hand_built_handoff_is_refused` from xfail to pass. `_FakeBackend` echoes its input
  **unsigned** and the suite calls that "signed" — fix together.
- **B3 / PR-4 · idempotency. UNBLOCKED by D-B** — a one-way digest may enter the spend ledger.
  The key must be **bound to transaction identity via that digest**; an unbound caller-chosen
  key is the PR-1-shaped hazard (one key reused across different transactions buys free
  authorizations), and binding is precisely what closes it. A pytest falsifier is now
  authorable: the same key against two different bindings must refuse, not replay.
- **X5 · absent-from-both-pre-and-post token accounts** read as measured-with-no-outflow.
  Documented at `simulate.py:20-29` and stopped — documentation is not a control. Needs agave
  row semantics and ALT-resolved key derivability measured first.
- **X7 · a fifth stale claim at `simulate.py:1157`** that STATE.md does not name. The known
  four: `:645-649` (the false cost claim), `:130`, `:314`, `spend_policy.py:111-114`.
- **N1 · non-value drain — SCOPE CORRECTED 2026-08-13.** The original wording here was
  MINE and is **false**: `sol_delta` is the fee payer's whole lamport change, so
  `per_transaction_cap_lamports` already charges fees AND rent. Measured on mainnet — the
  ATA-funding transfer `3t5u36XT…` shows −2,044,280 on the payer, of which **2,039,280 is
  rent-exemption**, visible to the cap. The REAL gap is (a) overhead is not budgeted
  APART from value, so one bucket covers a deliberate transfer and an incidental rent
  payment identically, and (b) nothing controls WHICH addresses may have an ATA created
  at the payer's expense. Scope N1 as those two, and check the existing measurement
  before designing any new control.
- **N2 · the `os-keychain` default profile has no backend.** Every real run must name a
  non-default today.
- **D1 · `let_me_buy`'s on-chain IDL** → lift three `manual` origins to `extracted`. $0, pure
  Python, `graph-engineer`.
- **D2–D4 · reference page**: invalid `petstore_openapi.json` (trailing comma, line 6); silent
  `to_arazzo` refusal; no `graph_data` contract; the two-tier `x-gecko-arity` extension and the
  upstream Arazzo PR (verified: no for-each in 1.0 or 1.1.0; the spec's own answer is index-0
  binding, which we refuse; 5 of 7 Pegana chains refuse).

---

## 7. Process rules — adopt now, two-way doors

1. **Every new or changed control ships with a counterexample drawn from an artifact we already
   hold** — for signing controls, the latest real mainnet transaction — demonstrated **RED
   before the fix**. This one rule catches PR-1, G1 and the R2 direction-flip.
2. **Every router/ranker change publishes the per-row served-set diff** for changed rows, never
   a summary statistic.
3. **Every ruling node re-measures its own subject first-party and never rules from a relayed
   table.** The relay defect is not in the codebase; it is in the dispatch graph.
4. **STATE.md is a lead, never evidence.** It was stale by nine commits this morning and named
   two gate subjects that no longer existed.

---

## 8. Standing constraints

`X402_MODE` stays `stub`. Mainnet signing is founder-authorized **per run**, never standing. A
fork is not mainnet and any output must say so. The only publishable mainnet CU figures are
36,508 and 36,399. `scripts/privy_fork_proof.py` is unmerged branch work — it is not evidence
about `main`.
