# Orquestra program flow — the gap map (derive → build → simulate → land)

**Date:** 2026-08-04
**Status:** Analysis (feeds the Program Catalog Graph spec). Merges three specialist reads:
defi-engineer (flows), web3-engineer (plumbing), solana-researcher (protocol evidence).
**Purpose:** depth-not-breadth — make a handful of programs *actually run* end-to-end and
name every real gap, before ingesting the catalog. Showcase = Berkay/Orquestra.

## The core reframe

**Deriving accounts is geometry; making a tx LAND is state.** Every gap is one of two
classes. Class 1 is the differentiator (the coding agent can't do it — the *IDL is wrong or
incomplete*); Class 2 is what turns a derived account set into a tx that confirms.

**The compose boundary, confirmed by all three:** Gecko **declares** (the missing/hidden
accounts, the corrected seed, the prelude, the compute-budget hints); **Orquestra builds**
the tx; **1claw signs** a *bound* receipt. Gecko never emits bytes, never signs, never
broadcasts.

---

## Class 1 — comprehension gaps (the IDL is wrong/incomplete) → OUR differentiator

| Gap | Program | What the surface says vs reality | Fix | Provenance |
|---|---|---|---|---|
| **`base_factor` 4th seed** ✅ | Meteora DLMM | Our `lb_pair` recipe used `[min,max,bin_step]` (3 seeds). SDK **deprecated** that (PR #49, merged 2024-05-09) for `derive_lb_pair_pda2` with **`base_factor:u16` (LE) as a real 4th seed**. **Old recipe derived the WRONG pool for any pool made after May 2024 — silently, no error.** Our demo pool (`5rCf1DM8…`) is a pre-upgrade survivor (`require_base_factor_seed==0`) → the bug was invisible. | **SHIPPED**: `lb_pair` = `[min,max,bin_step,base_factor(u16 LE)]`. Source (`commons/src/pda.rs::derive_lb_pair_pda2`) shows base_factor is a **plain function argument, NOT a PresetParameter read** — it selects among fee-tier pools sharing (mint-pair, bin_step), so it is **caller-supplied** (a variable seed like bin_step; the agent MUST specify the fee tier). Differential test pins current pool `EtAdVRLFH…` (`require_base_factor_seed==1`, base_factor=4000): old 3-seed ≠ address, new 4-seed = address, verified live on mainnet. | RECOVERED |
| **`bonding_curve_v2` hidden account** | Pump.fun | Required for `sell` (and post-Apr-2026 `buy`), but the **IDL never names it** — it only travels as a `remaining_accounts` entry, so every IDL-driven client silently omits it → revert. | Derive + inject as a conditional remaining-account (seed likely `["bonding-curve-v2",mint]`, MEDIUM-confidence → treat as flagged until source-verified). | RECOVERED / FLAGGED |
| **`lb_pair` root seed dropped** | Meteora DLMM | Anchor's IDL macro drops the whole PDA when a seed is a helper-fn (#4057) → Orquestra's own `/pda/derive` **400s** on `lb_pair`. | Recover the seed from source (shipped). | RECOVERED |
| **Graduation/migration** | Pump.fun | A `buy` plan against a **graduated** mint targets the wrong program (venue moved to PumpSwap via CPI at ~$69K mcap). | Detect graduation state → route/flag; don't build against the bonding curve. | FLAGGED |
| **Token-2022 metadata phantom** | Pump.fun | T2022 mints store metadata in-mint, not a Metaplex PDA — a naive tool derives a phantom account. | Already fixed (variable `token_program`; no metadata PDA). | shipped |
| **`fee_recipient` ambiguity** | Pump.fun | `Global` has a single field AND a 7-slot array AND (post-Apr-2026) a new appended slot — undeterminable from the surface. | Flag honestly; resolve empirically via the Receipt (try `global[41]`, let the sim confirm/refute). | FLAGGED |
| **`stake` cross-program** | ORE | `stake` is CPI'd into a *separate* program (`stakecNP…`), not ORE — a naive tool derives a plausible-but-wrong address under ORE's id. | Pin the cross-program id (shipped as a regression test). | RECOVERED |
| **MetaDAO version trap** | MetaDAO | Orquestra labels it "IDL v8" but the deployed program is `v07_launchpad`; its PDA Finder returns **zero** PDAs. | Recover from v07 source; flag foreign-program accounts (DAMM/Squads/Futarchy). | RECOVERED / FLAGGED |

## Class 2 — landing gaps (accounts right, tx won't land) → mostly compose, Orquestra builds

| Gap | Whose lane | Fix |
|---|---|---|
| **ATA-idempotent prelude (3012)** | Gecko **declares**, Orquestra **builds** | Check ATA existence (`getMultipleAccounts`), declare `createAssociatedTokenAccountIdempotent` prelude in the `/build` payload → Orquestra assembles a 2-instruction tx. THE fix that turns today's failing Receipt into a pass. |
| **Arg needs live state** | Gecko | `max_sol_cost` = decode `bonding_curve` reserves (same account we already read); `min_amount_out` = `lb_pair.active_id` price. Not Jupiter. |
| **`bin_array` account set** | Gecko declares | Meteora `swap` needs bin-array PDAs covering the active bin — derived from `lb_pair.active_id` (state read). |
| **Compute-budget + priority-fee** | Gecko surfaces, Orquestra builds | None exist. Receipt `units_consumed` → CU limit; `getRecentPrioritizationFees` → CU price. Required to land under load. |
| **Fresh blockhash + `lastValidBlockHeight`** | builder/lander; Gecko caveats | `replaceRecentBlockhash:true` means the Receipt attests nothing about land-time blockhash; the signed tx needs a fresh one at sign-time. Stamp the caveat on the Receipt. |
| **wSOL wrap/close** | Orquestra builds | Meteora SOL-leg only. **Not pump** (native SOL) — scope-creep trap. |
| **Signing-gate binding** | Gecko decides, 1claw enforces | Today `evaluate()` gates on tool identity + poison verdict only → a swap-accounts/amount attack passes. Add `evaluate_tx(receipt, approved_hash, presented_hash)` — fail-closed unless poison-clean AND sim-pass AND message-hash-match, single-use + time-bounded. |
| **v0/ALTs** | builder | Only when account count blows the 1232-byte legacy limit (large routes). Defer. |

---

## The compose verdict — answers "should we integrate RPCs + Pegana/Jupiter/Pyth?"

The four-partner framing **overclaimed.** Evidence supports **one** hard requirement and
three narrower/conditional ones:

- **RPC — the only universally load-bearing input, and we already do it.** `getAccountInfo`
  for resolution + `getMultipleAccounts` for ATA checks (our lane, control-plane reads,
  cache nothing). To *land*: an RPC with priority-fee/staked-send (Helius/Triton) — the
  operator's choice via `--rpc-url`, never hardcoded.
- **Jupiter — conditional, narrower than "price sanity."** It **does not route pump.fun
  bonding-curve tokens pre-graduation at all**; pre-graduation price is the bonding-curve
  read. Jupiter is real only post-graduation (PumpSwap) or as one router over Meteora.
- **Pyth — not load-bearing for any of the 4 base flows.** No source references a Pyth
  account in their instructions. A safety overlay above the plan, not a dependency.
- **Pegana — flagship-demo-only** (the depeg-signal → de-risk-swap composition, later).

**Bottom line: do NOT wire Jupiter/Pyth/Pegana into the base flows.** The real compose is
the operator's RPC + Orquestra building the preludes/compute-budget + 1claw signing the
bound receipt.

## Discovery / pain evidence (this is real, unprompted struggle — the light-up signal)

- **AnchorError 3012** on pump buys, hit in the wild: chainstacklabs/pump-fun-bot #28,
  pumpfun-bonkfun-bot #4.
- **Account-layout drift broke working bots twice in 2026** (pumpfun-bonkfun-bot #6, #158 —
  *"bot was working fine the previous day"*): the exact docs-go-stale / agent-guesses-wrong
  pattern our drift series catches.
- **Anchor dropping PDA seeds is a pattern, not a one-off:** solana-foundation/anchor #4057,
  #1550, project-serum/anchor #1004, #1641 — four tickets, one root complaint.
- **ORE authority/signer confusion → upstream fix** (ore-cli PR #150), matching our config
  note before we found the PR.
- **Meteora's own SDK deprecated its PDA scheme** (PR #49) — the upstream root of base_factor.

## Non-Anchor (ORE/Steel) degradation — the four-tier extraction ladder

1. Anchor IDL `pda.seeds` where present (missing-on-a-PDA-shaped account → **flag**, don't
   assume "not a PDA").
2. Source regex `#[account(seeds=[...])]` for legacy/gapped Anchor.
3. Source regex `*_pda()`/`find_program_address`/`has_seeds` for Steel/native (proven on ORE).
4. LLM **only** to propose a resolver node's deps/description — never to fabricate a value.

**Must FLAG (never fabricate):** runtime-data seeds (ORE `round` ← `board.round_id`), hashed
seeds, `remaining_accounts`-shaped requirements (the `bonding_curve_v2` class), cross-program
PDAs into **closed-source** deps. FLAGGED must survive every merge — that tag is the moat.

---

## The minimum real flow (the Berkay a-ha)

**A Pump.fun `buy` that returns `status: pass`** where derive-only returns the 3012 revert.
`plan_buy` graduates from an account dict to an ordered, landable bundle Gecko *declares*:
`[setComputeUnitLimit(from Receipt units), setComputeUnitPrice(from fee estimate),
createIdempotentATA(associated_user), buy(amount, max_sol_cost from curve read, track_volume)]`,
`fee_recipient = global[41]`, Orquestra builds it, surfpool simulates → **Receipt pass.**

The side-by-side that lands with Berkay:
- **Derive-only (today):** Orquestra builds → simulate → `account_error` (3012).
- **Gecko-complete:** same intent → prelude + curve-quoted arg + resolved fee_recipient →
  **pass.**

And the two Class-1 proofs make it un-wrapper-able: **base_factor** (the IDL derives the
*wrong pool* silently) and **bonding_curve_v2** (the IDL *hides* a required account). Neither
is discoverable from the surface the coding agent reads.

## How this maps to the catalog-graph deliverables

- **D-A (auto-comprehend):** the extraction ladder + merge; must reproduce hand-authored
  configs — **and fix `base_factor`** (a differential test would have caught it).
- **D-B (find_start showcase):** route intent → (program, instruction) + declare the prelude
  + the Class-1 recovered/hidden accounts. Lead the demo with base_factor / bonding_curve_v2.
- **D-C (verify-backed):** the pump-buy-that-passes Receipt + compute-budget + the bound
  signing-gate; drift series catches the next layout change (the "broke the next day" issue).

## Immediate correctness debt (independent of the graph work)
- ✅ **`base_factor`**: FIXED. `lb_pair` now uses the 4-seed `derive_lb_pair_pda2` scheme
  (`[min,max,bin_step,base_factor(u16 LE)]`); base_factor is caller-supplied (the fee tier),
  not a resolver read — the source refuted that hypothesis. Differential + live-mainnet
  proof against current pool `EtAdVRLFH…` guards against silent regression.
- **`bonding_curve_v2`**: pump `sell`/post-upgrade `buy` will revert without it; source-verify
  the seed before emitting, flag until then.
