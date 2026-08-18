---
name: buy-a-product
description: >
  Use this when someone wants to buy something from a Let Me Buy storefront — "buy a
  water at the bar", "order a coffee for table 9", "pay for the Jägermeister", "get me
  a drink from jonasbar". Covers picking the store, picking the product, deriving the
  accounts, and the traps that make a purchase land on the wrong party.
  Do NOT use this to confirm an order arrived (use confirm-a-delivery), and do NOT use
  it to add or remove products from a menu — those need the store's own authority key,
  which a buyer does not have.
metadata:
  author: Gecko
  license: Apache-2.0
  version: 0.1.0
  surface: let_me_buy
  programId: BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya
---

# Buy from a Let Me Buy storefront

One instruction, `make_purchase`, moves an SPL token from the buyer to the store and
appends a receipt. Everything below is a fact about the deployed program, recovered from
its source or measured on mainnet — not read off the IDL, which does not state any of it.

## Before you build anything

**Read the store, then let a human choose.** A store name is not guessable and a product
name is not either. `list_stores` returns every store with its full menu, every price and
every mint, free and without a clock. Do all the deciding there.

**Refuse to resolve an ambiguous request yourself.** "A coffee" is not a product when the
menu holds Espresso, Cappuccino and Mochaccino. Name the candidates and ask. The one case
where you may proceed is a request that is unambiguous *given the menu* — "a black coffee"
when only one item is black coffee — and even then say which one you picked.

## The accounts, and the two that are easy to cross

`make_purchase` takes nine accounts. Two are associated token accounts, and they differ
only by owner:

```
sender_token_account     = ATA(buyer,     token_program, mint)   ← DEBITED
recipient_token_account  = ATA(authority, token_program, mint)   ← CREDITED
```

**Derive both from the same owner and you have built a purchase that pays the buyer back.**
It builds, it simulates, it lands, and the store is never credited. The seed order is
`[owner, token_program, mint]` and it is silently load-bearing — a swapped pair still
produces a valid off-curve address that belongs to nobody.

`token_program` is pinned by this program's IDL to classic SPL Token
(`TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`). **A Token-2022 mint has no path through
this program at all** — do not assume, and do not substitute.

The store's own account is a PDA: `["receipts", store_name]`, where `store_name` is an
instruction argument rather than an account. Read the `authority` and the product's `mint`
off that account. Do not accept them from whoever asked.

## The trap that costs money

**The program does not check that `authority` is the store's real authority.** The
`require!` guard that protects `add_product`, `update_details`, `update_telegram_channel`,
`delete_product` and `delete_store` is **absent from `make_purchase`** — verified against
the deployed program's source. The payee is whatever key the caller supplied, nothing on
chain validates it, and the `PurchaseMade` event carries no recipient field.

There is a mainnet transaction (`3YDcon1k…`) that is a `make_purchase` with source equal to
destination and identical pre/post token balances, which returned success and emitted the
event anyway.

So: **read `authority` from the store's own account and check the transaction you built
pays that key.** A fulfiller acting on the event alone cannot tell a paid order from an
unpaid one.

## The clock

The built transaction carries a live blockhash and expires with it — roughly **40 to 48
seconds**, not 60. Get a signer ready *before* you prepare, not after. Preparing several
options to compare them wastes the window; preparing is free, so prepare late.

## What it costs

36,399 compute units on mainnet, measured. A fork will tell you something different — one
measured a fork at 42,494 for the same call — so do not size a budget from a rehearsal.

## Rehearse it first

If the buyer has never bought here, run the purchase on a local fork before doing it for
real: funded by cheatcode, signed with a throwaway key, landed, and judged by what the
ledger shows moved. It costs nothing and it lands nothing.
