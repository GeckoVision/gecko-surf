# Preparing the semantic scenarios for a mainnet run

**Status:** plan, 2026-08-20. Companion to `2026-08-20-semantic-run-plan.md`
(the fork path) and `2026-08-20-semantic-scenarios.md` (the scenarios).

**Boundary, stated first.** Claude PREPARES and hands over; the mainnet
broadcast is **founder-run, authorized per run** (project rule; `run_purchase`
does call `sendTransaction`). Nothing in this branch signs or sends to mainnet.
The deliverable here is the composition, the caps, the exact command, and the
dry-run-first gate — not an executed spend.

## What this session can and cannot see

Checked 2026-08-20 from the worktree: the local solana CLI is on **devnet**
(keypair `~/.gecko/wallets/gecko-dev.json`), and no mainnet/PayBox env vars are
set in this session. That is correct — **mainnet credentials must not live in a
worktree.** The mainnet signer is supplied by the founder at run time in the
main checkout (PayBox `WALLET_ID` in autonomous mode, or a mainnet keypair).
This plan therefore treats the signer as a founder-provided input, never a
thing this branch reads.

## The two orthogonal gates (this is the whole design)

A mainnet purchase must pass BOTH, and they answer different questions:

| Gate | Question | Where it lives | Blocks |
|---|---|---|---|
| **Semantic gate** (`gecko.semantic_gate`) | Is this the RIGHT spend for the user's intent? | above `run_purchase`, in the runner | wrong item/quantity/branch; non-authority destination; stale price; out-of-stock; duplicate — BEFORE anything is prepared |
| **Spend-policy gate** (`SpendPolicyGate`, held by the signer) | Is this spend within the money limits? | inside `run_purchase`; `signer.spend_gate is spend_gate` enforced | per-tx / hourly / daily USDC + SOL caps; max tx/day |

The semantic gate is the new layer; the spend-policy gate already guards every
`autonomous_purchase` run. Neither substitutes for the other: a semantically
correct spend can still bust the budget, and a within-budget spend can still be
the wrong item. The order is semantic-first (cheaper to refuse before building).

## Recommended caps for the coffee run

Coffee is cents. Set the spend-policy gate TIGHT so a bug cannot spend more
than a few orders' worth. With the store priced in USDC (geckocoffee's mint):

- `usdc_per_transaction_raw` = the most expensive scenario item + a little
  headroom (e.g. the dearest coffee ≈ 6.2 "units" in the catalogue → set to the
  seeded store's real dearest price × 1.1).
- `usdc_daily_raw` = the full three-scenario spend + headroom. Only
  **barista-order actually spends** (3 items); office-order and my-usual PASS by
  BLOCKING, zero spend. So the daily cap need only cover one barista basket plus
  a retry margin.
- `max_transactions_per_day` = 6 (three barista items + headroom), not the
  default 20.

The defaults (`0.01 SOL` / `1 USDC` per tx, `0.2 SOL` / `20 USDC` daily, 20
tx/day) are already safe for a coffee run; tightening is belt-and-suspenders.

## The sequence — fork green FIRST, then mainnet

1. **Seed geckocoffee** with the 31-item catalogue (`to_store_config()`) — the
   still-open blocker from the fork run plan. Path A (mainnet store, fork
   inherits) is what makes the mainnet run possible at all.
2. **Fork dry-run, all three green:**
   ```bash
   surfpool start --no-tui --no-deploy --rpc-url <mainnet-rpc> --port 8899
   uv run python scripts/semantic_run.py --rpc-url http://127.0.0.1:8899 --store geckocoffee
   ```
   Do NOT proceed to mainnet until this is 3/3. The fork uses the SAME store
   account bytes mainnet will, so a fork pass is real evidence, not a mock.
3. **Mainnet, founder-run, per scenario:** the founder, in the main checkout,
   with the mainnet signer configured and caps set, runs the scenario through
   `autonomous_purchase` with `--broadcast`. The semantic gate still runs first
   (it is in the runner, not the fork surface), so a mainnet run of my-usual
   still BLOCKS on the injected promo / stale price before any send.
4. **Verify by receipt**, never by claim: `sol_delta` / token deltas / the
   written receipt — the same discipline as the fork.

## The founder command (mainnet)

The mainnet spend goes through the existing, proven path
(`autonomous_purchase`, the one behind the 18 exact-CU mainnet txs), with the
semantic scenario supplying the resolved plan. Shape:

```bash
# main checkout, NOT a worktree; mainnet signer + caps configured by the founder
GECKO_REQUIRE_KEY=1 \
uv run python scripts/semantic_run.py \
    --rpc-url "$MAINNET_RPC_URL" --network mainnet \
    --store geckocoffee --scenario barista-order \
    --broadcast   # <-- founder-only flag; absent = simulate/dry-run
```

`--broadcast` is the founder authorization, per run, per the standing rule.
Absent it, the run simulates and hands back the unsigned bytes + the receipt
prediction. (Note: `scripts/semantic_run.py` today drives the FORK surface; the
mainnet `--network mainnet --broadcast` path routes the same resolved plans
through `autonomous_purchase.run_purchase` with the founder's signer — the small
surface adapter for that is the one code addition left, and it is a
follow-up PR so the auto-broadcasting code lands under its own review, never
mixed with the fork surface.)

## Why only barista-order spends — and why that is the point

Of the three scenarios, TWO pass by refusing before any spend:
- **office-order** blocks at the plan gate (oat vs budget conflict) — 0 tx.
- **my-usual** blocks at the purchase gate (out-of-stock water; the injected
  promo address never reaches a spend because the runner never proposes a
  non-authority destination) — 0 tx.

So the mainnet run risks real money on exactly ONE scenario (three small coffee
purchases), and the other two DEMONSTRATE the product by spending nothing. The
scenario most people would fear on mainnet — the fund-routing injection — is the
one that provably sends zero lamports. That contrast is the mainnet demo.

## Future: API-caller auth gates WHO may trigger a mainnet run

When this is the App surface, the run endpoint is a spend surface: the mainnet
`--broadcast` equivalent must sit behind the agent-identity login (OTP + SIWS,
keypair not bearer), so only an authorized caller can spend. The login gates
WHO; the semantic gate governs WHETHER the spend is right; the spend-policy gate
bounds HOW MUCH. Three gates, three questions, composed — none redundant.
