# `let_me_buy` — what we found, and what to send upstream

**Date:** 2026-08-14 · **Program:** `BUYuxRfhCMWavaUWxhGtPP3ksKEDZxCD5gzknk3JfAya` (mainnet)
· **Source:** `Woody4618/bar` (Jonas Hahn, Solana Foundation), osec-matched to the deployed
binary · **Status:** our own artifact corrected; nothing sent to the partner yet

Two specialists investigated independently — *"does CU grow per purchase, and would a real
shop hit a wall?"* — and converged. Everything below is checked against the verified source
AND against mainnet reads, not inferred from the IDL.

## THESE ARE TWO REPORTS, NOT ONE

Bundling them would bury the urgent one inside a performance write-up.

**Send first, privately: the missing authority check.** It is live on mainnet on a program
with 100+ storefronts.

**Send after, as a design discussion: the 20-receipt cap and its consequences.**

Nothing has been sent. Contacting the partner is the founder's call
([[confirm-before-outward-push-pr]]).

---

## Report 1 — `make_purchase` does not check the merchant

`require!(*authority.key == receipts.authority, InvalidAuthority)` guards `add_product`,
`update_details`, `update_telegram_channel`, `delete_product` and `delete_store`. It is
**absent from `make_purchase`**, whose accounts struct declares
`pub authority: SystemAccount<'info>` with only `mut`. The credited ATA is derived from
that caller-supplied key, and nothing on chain validates it.

**Behavioural evidence, not just source reading.** Mainnet `3YDcon1k…` is a `make_purchase`
where `source == destination` and the pre/post token balances are **identical
(14,033,833 → 14,033,833)** — nothing moved — and the program still returned success,
pushed a receipt, incremented `total_purchases` and emitted `PurchaseMade`. That
transaction was the merchant self-testing, so it is not an attack; it demonstrates the path
is accepted. Neither specialist attempted a third-party version, and neither should.

**Why it bites in practice:** `PurchaseMade` carries no recipient field. A fulfiller acting
on that event — the Telegram bot the program is built around — **cannot distinguish a paid
order from an unpaid one.**

Two smaller ones in the same family: `mark_as_delivered` carries no authority constraint
either (any signer may mark any receipt delivered), and the deployed binary does not
constrain `sender_token_account` although the IDL advertises ATA-shaped `pda` seeds for it,
so the IDL over-claims relative to the chain.

**This is precisely what `check_plan_accounts` defends against on our side** by reading the
authority from the store's own account. The docstring line *"the real one answered HTTP 200
to a purchase that paid the buyer back"* is now corroborated by the program's own source.

---

## Report 2 — the cap, and why CU is bounded

```rust
if ctx.accounts.receipts.receipts.len() >= 20 {
    ctx.accounts.receipts.receipts.remove(0);   // silent, no error/event/log
}
ctx.accounts.receipts.receipts.push(new_receipt);
```

The account is sized once at `initialize` to 3,681 bytes and **`make_purchase` never
reallocs**. So compute is bounded and saturating, not growing:

| store | purchases | receipts held | CU |
|---|---|---|---|
| jonasbar | **125** | 20 (saturated at #21) | **36,399, bit-exact flat** |
| geckocoffee | 7 | 7 | 19,214 → 24,956, climbing to the cap |

Measured model, cross-validated across both stores to **0.9%**:
`CU ≈ 19,780 + 12.7 × resident receipt bytes`.

**At 10,000 purchases nothing happens.** ~43,600 CU worst case against a 200,000
per-instruction limit; the 10 MB account cap and the 10 KiB/tx realloc limit are never
engaged. **A shop never outgrows the transaction. It outgrows its own memory.**

### The wall is data loss, at purchase #21

jonasbar has **lost 105 receipts**, silently. And `mark_as_delivered` scans the vec and
returns `Ok` when it finds nothing — so marking an evicted receipt **succeeds and does
nothing**. A venue with more than 20 open orders loses them from the delivery queue with no
error.

**Evicted buyer data survives in the account.** Anchor's `try_serialize` writes in place and
does not zero the remainder, and the account never reallocs: jonasbar carries 376 non-zero
bytes past the logical end of its payload, with a 62-byte contiguous run at the boundary —
the shape of a stale evicted `Receipt`. A decoder that reads the vec length stops (ours
does); a raw reader recovers pubkeys the program's own model says were deleted.

### Cheapest fixes, in order of blast radius

1. **`emit!` in the eviction branch** — zero layout change, zero CU change. Fixes the
   *harm* (nothing disappears without a trace) rather than the mechanism.
2. **`bump = receipts.bump`** — one line, no IDL or client change. `make_purchase` uses a
   bare `bump`, so Anchor re-derives the PDA on chain at 1,500 CU per iteration. Costs
   4,500 CU at jonasbar and **10,500 at `alexsbar`** — a quarter of its budget — to
   re-derive an address the struct already stores.
3. **Zero-copy ring buffer** (`[Receipt; 20]` + `head`, `AccountLoader`) — constant CU, well
   below today's floor, keeps the single read and the one-time rent. Costs a layout
   migration for ~100 live stores.
4. **Receipt-per-PDA** — removes the O(N) term but replaces one `getAccountInfo` with
   `getProgramAccounts` (the call free RPCs throttle), breaks read atomicity, and charges
   the buyer ~0.0018 SOL of unreclaimed rent per purchase — ~18 SOL dead at 10,000 sales.

Not a candidate: dropping the receipt and relying on `emit!`. Logs are not durable state,
most RPCs prune them within days, and `mark_as_delivered` would have nothing to mark.

---

## Report 3 — the fulfilment channel points nowhere, on several stores

`PurchaseMade` carries `telegram_channel_id`, cloned from the store account (source line
250). So it is not a label — **it is the delivery address**. Read from chain on 2026-08-14:

| store | purchases | `telegram_channel_id` |
|---|---|---|
| `jonasbar` | **125** | `@foolsgold_test` — a TEST channel, named after a different store |
| `geckocoffee` | 8 | `geckovision` — **does not exist** (ours; founder confirmed) |
| `superteamde` | 24 | `@superteamjaegermeister` |

The consequence is the same in each doubtful case: **the payment lands, the receipt is
written, and nobody is told to make the coffee.** A buyer has no way to see this before
paying, which is why `list_stores` now reports each store's `fulfilment` and flags an empty
one.

The honest limit, stated in the tool: an EMPTY channel is checkable from the chain; a
wrong-but-present one is not, and looks identical to a correct one. We report the value and
never endorse it.

Fixing a store is `update_telegram_channel`, which is authority-gated — so each merchant
fixes their own, and `jonasbar` is not ours to change.

## What we already corrected on our side

`gecko/providers/configs/orquestra/let_me_buy.json` claimed *"the program checks it against
receipts.authority (6004 InvalidAuthority)"*. **It does not.** That was a `manual`-tier note
that was simply false — the class of error that tier exists to admit. Corrected, with the
source it was checked against named, and the 20-cap plus silent eviction added, since the
config had not mentioned the single most load-bearing behavioural fact about the program.

Pinned by `tests/test_let_me_buy_config.py` so neither can quietly regress.

## What this means for our CU claims

**Our published numbers stand.** One specialist reported jonasbar at ~43,300 today and our
36,399 as stale; a live simulation returns **36,399**, twice, and the other specialist
explains the discrepancy — jonasbar's Jan–Mar transactions ran 42–48k because the SPL Token
CPI metered at 5,930 CU then versus 105 now, a runtime accounting change unrelated to this
program. **Only compare CU within a metering window.**

The published 36,508 → 36,399 lesson also has a sharper explanation than the one we shipped.
It is not "the first purchase wrote a record so the program does different work" — jonasbar
sells `Water` (5 bytes) and `Jägermeister` (13). Eight bytes × 12.7 CU/byte = **101 CU
predicted, 109 observed**. A Jägermeister receipt sat inside the 20-window during our first
purchase and the window rolled it off. The docs' conclusion (*a receipt is true for the
state it was taken against*) is unchanged and now better supported.

**And the argument against ever caching a CU number:** `init_if_needed` on the recipient ATA
is a ~20,000 CU cliff that fires on the first sale at any store whose merchant lacks an ATA
for that mint. Simulation catches it; a remembered constant does not. That is the case for
`simulate → Receipt` per call, stated in measurements rather than principle.
