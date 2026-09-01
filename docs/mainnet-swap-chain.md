# The USDG-only chain — five mainnet transactions, 2026-08-26/27

Five transactions that answer one question end to end: **can a wallet holding a token the
storefront does not accept still buy the coffee, without anyone guessing at any step?**

They are recorded here because `docs/mainnet-ledger.jsonl` needs a citable
`predicted_source`, and these five had none. Read the provenance note at the bottom before
quoting any prediction from this page — it is not the same grade of evidence as the two
rows backed by `README.md` and `docs/assets/coffee.cast`.

## The five

Every `charged_cu`, `slot`, `err` and fee below was read back from mainnet by
`scripts/mainnet_ledger.py`, not transcribed. The signatures are complete and checkable
on any explorer.

| # | signature | slot | predicted | charged | what it proved |
|---|---|---|---|---|---|
| 1 | `unFg5wYW6n7v9iKa7t7NvzAJS2rjME2EBwnRb8tdsSRTr1gCpCjWo3kqNracWSe5wNR8SBvEvtdVvyQGSoCAswj` | 441,778,607 | 41,615 | 41,615 | first DeFi swap, and the first mainnet transaction from an **auto-comprehended** program |
| 2 | `3szbhgFoFJ4NiNoziSt6ZsnWwwq9GxBF8TpDDN9C8FKYJXGw2ySaFBoB3Jm8UkADPoZEuxYpRUUumKjt5Dd3Qq19` | 441,797,280 | 41,607 | 41,607 | swapped the **remaining** USDC to USDG — "this wallet holds only USDG" became a fact on chain rather than a framing |
| 3 | `5c9KuuXjH2xVCwRitGV2oQJotRtaoU8WDG74CNpQXYt7MB3CDFkHKWgcvqzqrMHMbHMtrikBAiBdMTpe5KUZccFU` | 441,798,061 | 44,584 | 44,584 | USDG → USDC **a-to-b**, the direction that had never landed; downward tick arrays |
| 4 | `47bt1wUSw9RTqym5WRoPqM7zz4jb3gMgtgFbGvXjtkMdgCKNnfmZZxdmQ7vQwtqzYSKy5v4jGPQF6yvVnVzft6kS` | 442,020,951 | 44,827 | 44,827 | the same a-to-b swap from a **separate buyer** wallet: 250,000 USDG → 101,011 USDC |
| 5 | `2d62xnim4YqozhCQQCFcmidywvBWG5gyNtvA6n3ZFzspeeeFpi4rNGxDgpyTL8AssRyhgi3kiZitQrMMhuqo17i4` | 442,021,012 | 48,280 | 48,280 | **the espresso.** Buyer USDC 101,011 → 1,011; geckocoffee 3,971,003 → 4,071,003 |

All five: `err: null`, fee 5,000 lamports. Venue for every swap is one Orca Whirlpool pool,
`9RqDTfwCx2SgxsvKpspQHc38HUo3B6hRd3oR9JR966Ps`, derived from the mint pair rather than
chosen — see below.

## What the chain settled that an argument could not

**The venue is derived, and it proves itself.** Given (held mint, priced mint) the pool is
found by memcmp on the Whirlpool discriminator plus `token_mint_a`/`token_mint_b` at
offsets computed from the IDL — and then each candidate's address is **re-derived from its
own (config, mints, tick_spacing)**. A pool that does not reproduce its own address is
dropped. The memcmp proposes; the seed recipe disposes. That is what makes a wrong field
offset refute itself instead of returning a plausible wrong pool.

**Four well-formed wrong answers were caught by comparison, not by inspection** (all on
transaction 1):

1. The **pool address** — the catalog surface derived the 5th seed as a runtime resolver on
   `adaptive_fee_tier.fee_tier_index`, which made the pool underivable. It is a
   caller-supplied `u16` LE `tick_spacing`. A wrong tier yields a real, valid, **wrong**
   pool, silently.
2. **`tick_array_1` / `_2`** — seeded with the ASCII **decimal string**, which the surface
   encoded as `i32` LE because the IDL declares the argument as `i32`. An argument's IDL
   type does not determine its seed encoding.
3. The arrays are the **upward** set: b→a walks ticks up, a→b down, and only the array
   holding the current tick is shared between the two directions.
4. The **USDG account** derived under **Token-2022** — `derive_ata` had been defaulting to
   classic SPL, including in the agent-facing MCP tool.

Transaction 1 carries **mixed token programs in one instruction**: Token-2022 in, classic
SPL out.

**The self-purchase trap cost three transactions, and it is the reason there is a separate
buyer.** The wallet first funded with USDG *was* geckocoffee's own authority, so
`ATA(signer, mint)` and `ATA(authority, mint)` are one address and the payment would credit
the account it debits. `check_plan_accounts` refuses it — correctly — but by then the swaps
funding that wallet had already been paid for. The fix was a separate buyer paying our store,
which also makes the loop repeatable for fee cost only. The payability check now runs this
**before** balances, because ordering is the whole lesson: a route quoted before a structural
refusal is a route paid for and then thrown away.

**Rent arithmetic blocked setup twice.** A buyer's system account needs the rent-exempt
floor of **890,880** lamports simply to exist, and a USDG **Token-2022** account costs
**2,157,600** (182 bytes — the transfer-fee extension makes it larger than classic's
2,039,280 at 165). `--fund-recipient` creates the ATA and charges the sender, but does *not*
create the recipient's system account: the buyer's USDC ATA existed for months while the
wallet itself did not exist at all (`getAccountInfo` → `null`).

## What these five do NOT prove

- **Multi-array traversal is untested.** The pool holds roughly $25M at tick spacing 1, so a
  test-sized swap never leaves its starting tick array. That belongs on a fork, where it is
  free at any size.
- **The swap was not reachable by an agent when these five settled.** Every one was run by
  hand: `getTokenAccountsByOwner` appeared nowhere in the `gecko/` package, so nothing we
  shipped could see what a buyer held, and the decision path lived in `scripts/` where no
  tool could import it. **CLOSED 2026-08-30** — `gecko/pay_route.py` owns that decision, the
  `plan_payment` tool exposes it, and the script is a rendering of the same call. The five
  transactions above still predate it.
- **The router does not know a swap is needed** — and the reason is subtler than it looks.
  Asked *"buy an espresso at the coffee store but I only have USDG, not USDC"*, `find_start`
  returns `let_me_buy` five times. The mint names are **not** ignored: measured against the
  20 wired cards, `usdg` appears on exactly one card — Whirlpool, the only venue that can
  convert it — and `usdc` on four. Whirlpool scores 2. It loses to `let_me_buy/make_purchase`
  at 3, on `buy` + `coffee` + `store`.

  So the scorer counts raw overlap: `usdg`, which names 1 card in 20, is worth exactly as
  much as `buy`, which names 7. **The most discriminating word in the sentence is priced
  identically to the least**, and three vague tokens beat two exact ones every time. The
  fix is term weighting, not a synonym table — an alias list would move the number while
  leaving the agent unable to say which pool converts the balance it is holding.

## Provenance of the predictions — read this before quoting

`charged_cu` above is read from the chain and re-read by `scripts/mainnet_ledger.py
--verify`. It cannot drift.

`predicted_cu` is different, and weaker. These five predictions were recorded in the running
session's notes at the time of each run, and **the receipt output itself was not preserved**.
This page is a transcription of those notes; it is not an independent artifact, and it
cannot distinguish a number predicted before signing from one written down afterwards. The
five values were checked against the chain when this page was written and all five match
exactly — that establishes the transcription is accurate, and nothing about when the number
was formed.

The two rows in the ledger cited to `README.md` and `docs/assets/coffee.cast` are stronger:
in those cases the prediction was captured in an artifact before the transaction settled.

Quote `exact / with_prediction`, never `exact / landed`, and if the distinction between
these grades of evidence matters to a claim, say which rows back it.

**The fix is forward, not backward.** A prediction that exists only in a note is one nobody
can check later. Future runs should write the receipt to a file and cite that file, so the
question this section has to raise never comes up again.
