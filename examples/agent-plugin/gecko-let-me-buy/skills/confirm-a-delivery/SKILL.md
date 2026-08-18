---
name: confirm-a-delivery
description: >
  Use this when the store side needs to mark an order as delivered — "the coffee is on
  its way to table 9", "mark receipt 106 delivered", "tell the customer their order is
  ready", "close out that order". Covers finding the receipt, the argument-name trap,
  and the reason a delivery queue on this program silently loses orders.
  Do NOT use this to buy something (use buy-a-product). This instruction needs the
  STORE'S authority key, not the buyer's — a buyer cannot mark their own order delivered.
metadata:
  author: Gecko
  license: Apache-2.0
  version: 0.1.0
  surface: let_me_buy
  programId: BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya
---

# Confirm a delivery on a Let Me Buy storefront

`mark_as_delivered` takes two accounts — the store's receipts PDA and the authority — plus
a receipt id. It is signed by the **store**, never the buyer.

## The argument-name trap

The receipts PDA derives from `["receipts", store_name]`. But this instruction's own
arguments are named **`_store_name`** and `receipt_id` — with a leading underscore, because
Rust marks an unused parameter that way and Anchor copies the name straight into the IDL.

**So the seed points at an argument name that does not exist in the instruction that uses
it.** Bind the store name to `_store_name` on the wire and to `store_name` in the seed. A
tool that passes the IDL's seed path through verbatim derives nothing; a tool that renames
the argument to match the seed sends a field the program does not have.

This is the join an IDL loses, and it is why the recipe is carried separately.

## The trap that loses orders, and it is silent

**The receipts vec is capped at 20 and the account never reallocs** — it is fixed at 3,681
bytes. At the cap, `make_purchase` calls `receipts.remove(0)`: the oldest receipt is
**evicted with no error, no event and no log.**

One measured store has 125 purchases and holds 20. **105 receipts are gone from chain.**

The consequence for this instruction is the part to plan around:

> **`mark_as_delivered` on an evicted id SUCCEEDS AND DOES NOTHING.** It scans the vec,
> finds nothing, and returns `Ok`.

So a venue with more than 20 open orders loses them from the delivery queue invisibly, and
the program will keep telling you the delivery worked. **Never treat a successful
`mark_as_delivered` as proof the order was closed** — read the receipt back and check
`was_delivered` actually flipped.

If you are building a delivery queue on this program, the queue depth is 20. Hold your own
record of anything beyond it; the chain will not.

## Finding the receipt

Read the store's receipts account and match on the buyer, the product and the table number.
The vec is sequential and there is no index, so a walk reads every row — **every other row
belongs to a real customer of a real storefront.** Compare and discard; do not carry other
people's rows anywhere.

## What it costs

Compute is not a constant here, because the instruction scans the vec. It tracks the bytes
resident in the account, so it moves as the store trades — one store measured 34,858 and
then 34,137 CU a day apart, correctly, because the store's contents changed. **Do not pin a
budget to a number you measured last week**, and do not read a change as a regression.

## What the IDL says that is not true

The error table declares **`StoreNotEmpty` (6005)**, which reads like a guard on deleting a
store that still has products. **The deployed program never raises it** — verified against
source. `delete_store` checks the authority and then zeroes the account's lamports,
whatever it holds. A flow that trusts that guarantee deletes a live storefront and its
history.
