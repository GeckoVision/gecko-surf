# What stopped a hosted agent from doing today's run

Notes taken while landing a real mainnet leg on 2026-09-08: fund a wallet with USDG,
swap it to USDC, buy an espresso. Every gap below is one I hit, not one I imagined.
The target: Claude on the web does this once, through our MCP, with no local shell.

## What the run actually took

| # | Step | Tool used | On our MCP surface? |
|---|---|---|---|
| 1 | Read SOL + token balances across 3 wallets, 2 token programs | ad-hoc script | no |
| 2 | Discover we hold zero USDG | ad-hoc script | no |
| 3 | Read the USDG mint's extensions to confirm fee and hook are inert | ad-hoc script | no |
| 4 | Swap 1 USDC -> USDG | `scripts/prepare_whirlpool_swap.py --send` | partly |
| 5 | Transfer USDG to the buyer, creating its ATA | **`spl-token` CLI** | **no** |
| 6 | Swap 0.25 USDG -> USDC | `scripts/prepare_whirlpool_swap.py --send` | partly |
| 7 | Buy the espresso | `scripts/autonomous_purchase.py` | yes |
| 8 | Read both back, compare predicted vs actual CU | ad-hoc script | **no** |

Six of eight steps left the surface. A hosted agent can do step 7.

## The gaps, ranked by what they block

**1. Nothing checks whether the buyer holds the mint.** `list_stores` returns `mint` and
`token_program`, and its own description tells the caller to "Check the buyer's holding
against `mint` AND `token_program` before preparing". No tool does it.
`gecko.pay_route.plan_payment_result` does not either. So the first question of every flow
is answered by hand-rolling two `getTokenAccountsByOwner` calls with the right `programId`,
and getting the program wrong returns an empty list rather than an error. This is the
highest-value gap: it is the question that decides whether a swap is needed at all.

**2. There is no token transfer or wallet-provisioning tool.** Step 5 had to be the
`spl-token` CLI, with `--program-id TokenzQd…` and `--fund-recipient`. Any flow that
provisions a wallet is entirely off-surface, and the Token-2022 program id is exactly the
detail a caller omits.

**3. Nothing verifies a transaction after it lands.** `verify_signed_transaction` is
pre-broadcast. Reading a landed signature back and comparing predicted CU to actual CU is
the number the whole pitch rests on, and today I wrote it by hand for the third time.
`docs/mainnet-ledger.jsonl` records the prediction; nothing closes the loop.

**4. The ordering is tribal knowledge.** Swap before buy. Both ATAs must exist before
`swap_v2` runs, because it creates neither. A Token-2022 mint needs its program id at every
call site. `find_start` routes an intent to one program; nothing sequences two.

**5. Refusals name the fault, not the fix.** A 0-SOL buyer gets `receipt-failed` from
`prepare_purchase`, and `AccountNotFound` from the swap pre-flight, because a fee payer with
no account cannot be simulated. Both are correct and neither tells the agent it needs SOL or
a sponsor.

**6. The SSRF guard blocks loopback.** `prepare_purchase_result` refuses `127.0.0.1` unless
a caller injects `url_guard=_only_this_surfnet(proof)`. Correct for a hosted surface, and it
means a hosted agent cannot drive a local fork. The rehearsal seam exists; it is not reachable
over MCP.

**7. Two builder defects, both upstream.** The Orquestra builder fetches its own blockhash
from mainnet, so a fork run must recompile the message or get `BlockhashNotFound`. And when
`feePayer` differs from the instruction signer it emits `num_required_signatures = 2` with a
one-slot signature array, which the runtime rejects as `SanitizeFailure`. Reproduced on both
the swap and the purchase build.

## The shape of the fix

One tool that answers "can this buyer do this, and in what order", before any clock starts.
Given an intent and a buyer it returns: what the buyer holds, what the store wants, whether
those match, the ordered steps if they do not, and which accounts are missing. It reads only
and starts no blockhash clock, the way the keyless `prepare_purchase` already does.

That plus a transfer tool (gap 2) and a read-back tool (gap 3) turns six off-surface steps
into zero. Nothing here needs a new signer: every step still ends in unsigned bytes plus a
receipt, and the signer stays whoever the caller enrolled.

## Evidence from the run

Three transactions, all predicted before signing and exact on chain:
`Q4J9Q2Ck…` 41,816 CU, `4XgsSDS8…` 45,043 CU, `3oXRbDYN…` 48,409 CU. Ledger now 53 rows,
38 carrying a prediction. Verified-exact record moves from 15 of 15 to 18 of 18.
