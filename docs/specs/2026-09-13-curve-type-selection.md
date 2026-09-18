# Choosing the curve, not the venue — the GeckoCoffee ladder

**Gate: nothing here starts until the bootcamp ships.**

## The thesis

The agent is not picking a brand. It is picking a **curve type**, and the user never
notices. "Which DEX" is a consequence; "which shape of liquidity, for this pair, at this
size" is the decision. The difference matters because the curve type determines what
accounts the call needs — ticks in the direction of travel, bins, a constant-product pool,
or no pool at all — which is exactly the part an agent cannot guess.

Two user problems drive it, and neither is a commerce problem. Both are "I have money and
cannot spend it":

1. **No gas.** You hold the value, not the fee token. (Four USDC stranded on Ethereum for
   want of ETH is the same bug as needing SOL to move USDC.) Kora's own headline: *users
   never need SOL.*
2. **Wrong stablecoin.** You hold USDG, the store wants USDC. The agent asks **"convert?"**
   — never "swap". Swap is our word; convert is the user's.

## The type glossary, and what we have wired against each

| Type | What it is | Ours | Verdict today |
|---|---|---|---|
| CPMM | Constant product `x·y=k`, liquidity from zero to infinity. Simplest to call. | Raydium | **not wired** |
| CLMM | Concentrated. LPs pick a range; liquidity lives in **ticks**, so a swap must name the right tick arrays *in the direction of travel*. | `whirlpool.swap_v2` | **ok** |
| DLMM | Meteora's variant. Liquidity in discrete **bins** — zero slippage inside a bin, dynamic fees. | `meteora.swap` | **weak** |
| Bonding curve | No LPs. Price is a function of supply; the program is the counterparty. | `pumpfun.buy/sell` | **blocked** |
| Aggregator | Holds no liquidity. Routes across venues, hands back a route or a built transaction. | `jupiter.route` | **ok** |
| Orchestrator | Holds no liquidity. Wraps execution: destination routing, SOL wrapping, referrals, fees, limit orders. | Orquestra | catalogued |

We hold a specimen of every type on the board. That is the asset: the comparison is ours to
make because we can already call four of the six.

## The ladder — three swaps of rising difficulty, three curve types, one purchase

Each rung must make the agent **say why**, out loud, in the user's terms.

**Rung 1 — USDG → USDC on Orca (CLMM).** Stable-to-stable. The reason is not price: both
legs are pegged, so the interesting part is that USDG is **Token-2022** and USDC is classic
SPL. The token program is the second ATA seed, so getting it wrong derives a well-formed
wrong address. *Works today — `whirlpool.swap_v2` is `ok` and has landed real mainnet swaps.*

**Rung 2 — SOL → USDC on Raydium (CPMM).** The deepest pair on Solana and the simplest
maths, so the honest reason to choose it is depth, not cleverness. It also carries the
wrapped-SOL handling every other rung avoids. *Not wired: there is no Raydium provider
module. The seed work is already done — `amm_config` is big-endian, proven 8/8 against live
accounts vs 1/8 for little-endian, and that one match is index 0 where both encodings are
the same two zero bytes.*

**Rung 3 — BONK → USDC on Meteora (DLMM).** A thin book, where price impact is real and the
bin structure is the reason to choose it: zero slippage inside a bin. This is the rung that
proves the agent is reasoning about liquidity shape rather than reading a leaderboard.
*Was weak: `bin_array` carried an assumed byte order the IDL cannot state. Reproduced
2026-09-18 on pool `5rCf1DM8…` index −81: `le` derives `HQH5fsUp…`, which exists; `be`
derives `HdSrqcyp…`, which does not. The recipe is declared in the overlay with the
measurement (origin `manual`); the rung is **ok**.*

**Rung 4 — buy the espresso on `let_me_buy`.** *`make_purchase` is `ok`.*

Then: offer the neighbours in the category — a cortado, a flat white — because the agent
read the store, not a hardcoded list.

## Why this is more than venue selection

An aggregator answers "cheapest route." We answer a different question, and it is the one
nobody else is answering: **"can I call this correctly, and can I say why I chose it?"** The
answer names the curve type, the account it had to derive, and the evidence that the
derivation is real. That sentence is the product:

> *Orca, because both legs are stable and I can derive all nine accounts — including the
> Token-2022 program as the second ATA seed. Raydium would be a guess: its `amm_config`
> seed is unverified.*

## What blocks it, in the order it must be fixed

**0. The signer-slot trap — fund safety, before anything gasless.**
`gecko/prepare_instruction.py:172-187` pours `payer` into an instruction's lone open signer
slot. Name Kora as payer on a swap whose lone open signer is the token authority and **Kora
receives the swap authority.** This must close first. It is not a Kora task; it is a
correctness bug that Kora would weaponise.

**1. Gasless is not true yet.** `gecko/prepare_purchase.py:772` and `:895` hardcode
`"feePayer": buyer`. Every coffee transaction we have landed paid its own SOL. The best line
in the story is the one part not yet real.

**2. One venue is not a choice.** `gecko/pay_route.py:828-840` hardcodes
`idl_fetch("whirlpool")`. Its outcomes express *whether* to convert, never *where*. Until a
second venue is callable, "the agent picks the right path" means "the agent picks the only
path."

**3. Rung 3's byte order.** Done 2026-09-18: `meteora.bin_array` derived both ways against
a live account, `le` exists, `be` does not, declared in the overlay with the measurement.

**4. Rung 2's venue.** Wire Raydium CPMM. The seed is proven; this is the provider module,
the cards, and the intents.

## Sequence, gated

```
BOOTCAMP SHIPS  ── nothing below starts before this
      │
      ├─ 0. signer-slot trap            fund safety, blocks everything gasless
      ├─ 1. meteora bin_array           one live address; unblocks rung 3
      ├─ 2. Raydium CPMM wired          unblocks rung 2; makes pay_route a choice
      ├─ 3. pay_route: venue is an ARGUMENT, not a constant
      ├─ 4. Kora stage 2: feePayer != buyer   makes "never needs SOL" true
      └─ 5. shoot the ladder
```

Against `docs/specs/2026-09-11-all-calls-working.md`: items 1 and 3 there (the four assumed
byte orders, the two resolvers) are the same work as items 1 and 2 here, approached from the
demo instead of from the score. `meteora.bin_array` appears in both lists. Do it once.

## Out of scope

- **Best execution / price routing.** Ruled callable-correct, not cheapest. Quoting every
  venue is an aggregator's job.
- **Anything that reads as a commerce platform.** The coffee is the proof, not the product.
  Bido and PayBox operate where a catalog already exists; we operate where there is none.
