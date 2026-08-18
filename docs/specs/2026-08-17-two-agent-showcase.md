# The showcase: two agents, one storefront, and the orders that vanish

**Status:** design, 2026-08-17. Not built. Every defect it turns on is measured and cited.

---

## The idea, and the reason it is better than a happy path

A waiter agent takes orders and buys. A kitchen agent confirms deliveries. Two agents, one
storefront, on a local fork so nothing costs anything.

The obvious version of this demo shows two agents cooperating and a transaction landing. That
demo has been made a hundred times and it proves nothing, because **a happy path cannot fail
in front of an audience**, which means the audience learns nothing about when to trust it.

The version worth building turns on a defect the program actually has:

> **The receipts vec caps at 20 and evicts the oldest silently — no error, no event, no log.
> `mark_as_delivered` on an evicted receipt SUCCEEDS AND DOES NOTHING.**

So a delivery queue built on this program loses orders past the cap, invisibly, while the
program reports success. One measured store has **125 purchases and holds 20** — 105 receipts
are gone from chain.

**That is the demo.** Two agents run a plausible service, orders start disappearing, and
nothing in the system says so. Then the same run with the graph in front of it, which knows
the cap, and the queue holds.

## The shape

```
  the waiter agent                      the kitchen agent
  ────────────────                      ─────────────────
  find the store, read the menu         watch for receipts
  refuse to guess "a coffee"            mark_as_delivered
  rehearse the purchase                 CHECK was_delivered flipped
  buy                                   ← the check is the whole point
```

Both run against a surfpool fork of mainnet: two ephemeral keypairs, neither of which can
exist until the endpoint has proved it is a fork, funded by cheatcode, and thrown away after.
Nothing on mainnet, nothing anyone owns.

**The kitchen agent needs the store's authority**, not the buyer's — `mark_as_delivered` is
signed by the store. That is a real asymmetry rather than a staging convenience, and it is
another reason to run it on a fork: two distinct signers, both free.

## The three moments

**1. It refuses.** The waiter is asked for "a coffee" and the menu holds three. It names the
candidates and stops. This is the least dramatic moment and the most important one — an agent
that guesses here is an agent that buys the wrong thing confidently.

**2. It rehearses.** Before the first real purchase, the whole thing runs on the fork: funded,
signed, landed, and judged by what the ledger shows moved. The buyer's balance falls by
exactly the price and the store's rises by exactly the price, or it is a finding.

**3. The orders vanish.** Past 20 open receipts, `make_purchase` starts evicting. The kitchen
agent marks an evicted order delivered, gets `Ok`, and the order is gone. Run it twice: once
with a queue that trusts the program, and once with a queue that reads `was_delivered` back
and holds its own record past the cap.

## What it must not claim

- **The fork's compute is not mainnet's.** Measured: 42,494 on a fork against 36,399 on
  mainnet for the same purchase. Any number on screen says which chain it came from.
- **This is not a bug we found in a partner's product.** It is the behaviour of a third-party
  program that both we and they read. The demo shows a class of defect an IDL cannot express,
  not a failure of anybody's catalogue.
- **Two agents cooperating is not the achievement.** Any framework does that. The achievement
  is the one that notices the silence.

## What has to exist first

| | |
|---|---|
| the rehearsal | **done** — `gecko/sandbox/`, funded, signed, landed, judged |
| the two skills | **done** — the plugin's `buy-a-product` and `confirm-a-delivery` |
| the graph that knows the cap | **done** — it is in the config notes and the plugin's score |
| two agents driving them | **missing** — this is the build |
| a fork with 20+ receipts | **missing** — reachable with cheatcodes, or by 21 rehearsed buys |

The agent framework is the only genuinely new piece, and it is deliberately the thinnest part:
the skills already carry the knowledge, so the agents mostly need to read them and act.

## Where this goes after `let_me_buy`

The same two-role shape fits anything with a producer and a consumer, and the eviction defect
is specific to this program — so the second surface will need its own finding rather than this
one. **That is the point rather than a limitation**: a showcase whose interesting moment is a
real defect cannot be copy-pasted, and a scan that finds the next one is the thing we are
building anyway.
