# Live proofs — the E2E side-by-sides, verbatim

The five landing-orchestrator numbers the architecture cites (pump buy `86,669 CU`,
Meteora `81,964 CU`, MetaDAO `44,476 CU`, ORE `41,023 CU`, pump sell `50,783 CU`) come
from real env-gated E2E runs. With pump `sell` wired, **all four target programs have at
least one runnable executable intent** and pump has the full round-trip (buy → sell).
This file is their durable
record: the verbatim output, the date, and exactly how to re-run each. Honesty
labels: every run is a **surfpool mainnet fork** (a mainnet-backed state snapshot —
**NOT mainnet**), simulation only (`sigVerify:false`), $0, nothing signed or
broadcast, nothing stored.

## Pump.fun `buy` — naive derive-only ❌ 3012 → Gecko landing bundle ✅ 86,669 CU

Run 2026-08-05 (PR #277; same measured outcome as the first run in #276): real
Orquestra `/build`, surfpool mainnet fork, real mint. The naive path — the correct
derive-only `buy` a coding agent gets from the IDL — reverts on the buyer's
uninitialized ATA; the Gecko-complete bundle (curve-quoted `max_sol_cost`,
recovered `bonding_curve_v2` + the 8 `Global.buyback_fee_recipients@741` remaining
accounts, idempotent-ATA prelude, compute budget) passes.

Verbatim verdict block (`test_buy_that_passes_e2e_side_by_side`):

```
=== pump buy: naive derive-only vs GECKO landing bundle ===
❌ NAIVE (derive-only) — EXPECTED revert, this is the gap: account_error (3012)
✅ GECKO landing bundle — PASSES: 86,669 CU
RESULT: the naive path reverts on mainnet; the Gecko bundle lands — caught for $0 before any spend.
```

Re-run (needs `surfpool` on PATH + a mainnet RPC; `GECKO_MAINNET_RPC`,
`GECKO_E2E_USER`, `GECKO_E2E_FEE_RECIPIENT` optional overrides):

```bash
GECKO_SIMULATE_E2E=1 uv run pytest \
  tests/test_pump_buy_landing.py::test_buy_that_passes_e2e_side_by_side -s
```

## Meteora DLMM `swap` — derive-only ❌ 3012 → Gecko landing bundle ✅ 81,964 CU

Run 2026-08-05 (PR #279): real Orquestra `/build`, surfpool mainnet fork, the deep
SOL/USDC pool (`BGm1tav…`, bin_step 10 / base_factor 10000 — a real 4-seed pool).
The native-SOL leg exercises the full bundle: both ATAs idempotent, wSOL wrap
(transfer + SyncNative), the swap with the 3 bitmap-selected `bin_array` remaining
accounts the IDL never names, CloseAccount unwrap — one unsigned simulated tx.

Verbatim (`test_swap_that_passes_e2e_side_by_side`; log lines elided as in the PR
record):

```
=== meteora swap: derive-only vs Gecko-complete landing bundle ===
expected_out=73851 min_amount_out=70158 cu_limit=98356
bin_arrays=[-38, -39, -40] -> ['DKmQ4WQJm5Xkxwo9fcNmWknn18qUWyhLB5UDW4Vwmocv', '7mDb6YRqghMiTXU9J8xoHvgDzvvMLj6Q6X1aTumt6RFr', '3QZuWNu7JHmV4uZaeNqzZre7TmpQmdvDhrs436G35oXG']
DERIVE-ONLY (no ATAs/wrap/bin_arrays): status=fail revert_class=account_error
  ... 'Program log: AnchorError caused by account: user_token_out. Error Code: AccountNotInitialized. Error Number: 3012.' ...
GECKO-COMPLETE (landing bundle): status=pass units=81964 revert_class=None
  ... 'Program log: Instruction: CloseAccount' ... 'Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA success'
1 passed in 17.01s
```

Re-run (same prerequisites; `GECKO_E2E_METEORA_BIN_STEP` /
`GECKO_E2E_METEORA_BASE_FACTOR` optional overrides):

```bash
GECKO_SIMULATE_E2E=1 uv run pytest \
  tests/test_meteora_swap_landing.py::test_swap_that_passes_e2e_side_by_side -s
```

## MetaDAO launchpad `fund` — Gecko landing bundle ✅ 44,476 CU on a currently-Live launch

Run 2026-08-05: real Orquestra `/build`, surfpool mainnet fork, against a launch the
test DISCOVERED as genuinely fundable at run time (state `Live`, fund window open) and
a real funder of that launch whose USDC ATA still held the amount. The comprehension
gap here is upstream of the preludes: Orquestra's PDA Finder reports **zero** PDAs for
this program (no seeds in IDL `pda.seeds` — they live only in v07 source), and the
USDC vault is not derivable at all (`has_one = launch_quote_vault` — a field READ from
the launch account). Honesty note, verbatim in the output: because the discovered
funder already held the USDC ATA, the derive-only path (accounts already Gecko-filled)
also landed — the differential for this program is the account set itself, not the
ATA prelude. `funding_record` needed no init: the program `init_if_needed`s it
(source-verified).

Verbatim verdict block (`test_fund_that_passes_e2e_side_by_side`):

```
=== metadao fund: naive derive-only vs GECKO landing bundle ===
launch state=Live window_open=True amount=10000 (USDC base units)
  Live; fund window open until unix 1785945601
✅ NAIVE (derive-only) also lands — this funder already holds the USDC ATA; the fund gap is deriving/reading the account set at all (the IDL carries ZERO PDA seeds), not the prelude
✅ GECKO landing bundle — PASSES: 44,476 CU
```

Re-run (needs `surfpool` on PATH + a mainnet RPC; discovery is automatic, or pin
`GECKO_E2E_METADAO_BASE_MINT` / `GECKO_E2E_METADAO_FUNDER` / `GECKO_E2E_METADAO_AMOUNT`;
skips with the exact reason if no launch is fundable at run time):

```bash
GECKO_SIMULATE_E2E=1 uv run pytest \
  tests/test_metadao_fund_landing.py::test_fund_that_passes_e2e_side_by_side -s
```

## ORE `claimOre` — naive builder instruction ❌ privilege escalation → Gecko landing bundle ✅ 41,023 CU

Run 2026-08-05: real Orquestra `/build`, surfpool mainnet fork, against a miner the test
DISCOVERED at run time as holding accrued rewards (`getProgramAccounts` over the two
balance fields; skips honestly if none is claimable). `claim` — not `mine` — is the
runnable agent flow: mining needs an off-chain drillx PoW solution neither Gecko nor a
builder computes.

This is the cleanest differential of the four, and it is **not** about preludes: `claimOre`
needs none (the program creates the recipient ATA itself). The builder's own instruction
does not land. ORE is a Steel program; the `api/idl.json` it ships (`metadata.origin:
"steel"`) marks `board` `isMut: false`, and `/build` emits it read-only — but `claim_ore`
ends with `program_log`, a self-CPI that takes the board as a **writable** signer. So the
naive instruction transfers the tokens and *then* dies. `sdk::claim_ore` marks the board
writable, and every real mainnet `claimOre` carries it writable; Gecko reconciles the meta
from source (widening writability only — never signer-ness) and the bundle passes.

Verbatim verdict block (`test_claim_that_passes_e2e_side_by_side`):

```
=== ore claim: naive derive-only vs GECKO landing bundle ===
discovery: miner 3iRVwgvcTJCj6qCK3hcBhyZiXDNzmmBKwcryR6tjJjGw holds 283645877668295 ORE base units
authority=5D1oAw6sE14YvdSPwGWGbFCj7SVTbivnTxWXxbvNrur6 bps=10000 checkpoint_pending=False
claimable=2836.458777 ORE → pays out at least 2665.946201 ORE (refining fee 170.512576, 11 decimals)
❌ NAIVE (builder metas verbatim, board read-only) — FAILS: other
✅ GECKO landing bundle — PASSES: 41,023 CU
gecko corrected account metas from source: BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi
```

The naive failure, verbatim from its `logs_tail` — note it fails *after* the transfer
succeeds, which is why no static surface reveals it:

```
Program log: Claiming 2799.067287878 ORE. Paid 170.51257590283 ORE in refining fees.
Program Tokenkeg... invoke [2] / Instruction: Transfer / success
BrcSxdp1nXFzou1YyDnQJcPNBNHgoypZmTsyKBSLLXzi's writable privilege escalated
Program oreV3EG... failed: Cross-program invocation with unauthorized signer or writable account
```

Two more measured gaps ride along in the plan, declared not fixed: the optional `bps`
argument (`ClaimORE { bps: [u8; 8] }` — a *partial* claim) that the surface declares
`args: []` and `/build` drops even when supplied, so a "claim half" ask would silently
claim everything (the orchestrator refuses to simulate it); and `checkpoint`, claim's
precondition, which takes 8 accounts on mainnet (193/193 sampled) against the 6 the
surface declares. ORE has **11** decimals, and the `Miner` layout in the shipped IDL is
536 bytes against the deployed struct's 744 (every mainnet miner account is 752 = 8 + 744)
— an IDL-driven decoder reads the claimable balance at the wrong offset.

Re-run (needs `surfpool` on PATH + a mainnet RPC; discovery is automatic, or pin
`GECKO_E2E_ORE_SIGNER`; skips with the exact reason if no miner holds claimable rewards):

```bash
GECKO_SIMULATE_E2E=1 uv run pytest \
  tests/test_ore_claim_landing.py::test_claim_that_passes_e2e_side_by_side -s
```

## Pump.fun `sell` — naive builder instruction ❌ 6074 → Gecko landing bundle ✅ 50,783 CU

Run 2026-08-05 (PR for `feat/pump-sell-flow`): real Orquestra `/build`, surfpool mainnet
fork, real mint, and a holder **discovered at run time** (`getTokenLargestAccounts` →
first owner that is on-curve *and* whose token account is its canonical ATA; a sell cannot
be simulated from a wallet that holds nothing, and the test skips rather than fake one).

This is the "the shape lives in PROSE" case. The shipped IDL and the live builder surface
both list **14** accounts for `sell`; the only hint about the rest is the instruction's
English doc-comment — *"For cashback coins, pass as remaining_accounts:
[0] user_volume_accumulator, [1] bonding_curve_v2"*. The real instruction is **16
accounts** for a normal coin and **17** for a cashback coin, and which one you need is a
STATE read (`BondingCurve.is_cashback_coin`, bool @ data offset 82), not a caller choice.
Like ORE, the naive bundle is not a near-miss: it runs the token `TransferChecked` and
*then* reverts.

There is **no ATA prelude** here, and that is the honest answer rather than an omission —
the seller already holds the token, so `associated_user` exists; and the curve pays out
native lamports, so there is no wSOL wrap either. The whole prelude is ComputeBudget.

Verbatim verdict block (`test_sell_that_passes_e2e_side_by_side`):

```
=== pump sell: naive derive-only vs GECKO landing bundle ===
holder discovered at run time: 2sGTGW2EavV6mZ8irHiV5TsKpR3vnGiMcACNJgD6dFQq (balance 2,833,920,475,107)
❌ NAIVE (builder's 14-account sell) — EXPECTED revert, this is the gap: custom_program_error:6074 (6074)
✅ GECKO landing bundle — PASSES: 50,783 CU
RESULT: the naive path reverts on mainnet; the Gecko bundle lands — caught for $0 before any spend.
--- details ---
shape read from BondingCurve.is_cashback_coin@82: non-cashback (16)
amount=28339204751 base_sol_output=801481 min_sol_output=761406 cu_limit=60939
```

The naive log tail names the gap exactly:

```
Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb invoke [2]
Program log: Instruction: TransferChecked
Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb success
Program log: AnchorError thrown in programs/pump/src/sell.rs:126. Error Code: InvalidBondingCurveV2. Error Number: 6074. Error Message: bonding_curve_v2 remaining account is missing or invalid.
Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P failed: custom program error: 0x17ba
```

Two more measured facts ride along. `min_sol_output` is **not** the buy formula reversed:
the sell quote is `amount * virtual_sol_reserves // (virtual_token_reserves + amount)`
(denominator ADDS the input, no `+1`, no `real_token_reserves` cap) and the program
*subtracts* the protocol + creator fee from the proceeds, so the slippage guard must
shrink the floor where a buy's pads the ceiling — source:
`@pump-fun/pump-sdk@1.36.0 src/bondingCurve.ts getSellSolAmountFromTokenAmount(Quote)`.
And **mayhem-mode** coins (`is_mayhem_mode` @ offset 81) are FLAGGED, not claimed: on the
one mainnet mayhem sell inspected, the account in the `bonding_curve_v2` slot did not
match the standard `["bonding-curve-v2", mint]` derivation.

Re-run (needs `surfpool` on PATH + a mainnet RPC; holder discovery is automatic,
`GECKO_E2E_SELL_MINT` / `GECKO_E2E_FEE_RECIPIENT` optional overrides; skips with the exact
reason if no usable holder exists):

```bash
GECKO_SIMULATE_E2E=1 uv run pytest \
  tests/test_pump_sell_landing.py::test_sell_that_passes_e2e_side_by_side -s
```

## Recording an outcome from a re-run (opt-in)

Any of the orchestrators (and the Path-A `simulate` MCP tool) can append the run's
categorical outcome — status, revert family + public code, compute units, slot,
network category, values-free `recipe_hash`; never a pubkey/amount/log — to a
segregated `simulated.jsonl` via the explicit `record_to` opt-in (default: record
nothing). Read the series back with:

```bash
uv run gecko drift path/to/simulated.jsonl   # exit 0 = stable, 1 = drift detected
```

CU numbers are **measured** per run against a fork snapshot and can vary slightly
with on-chain state; the side-by-side verdict (naive revert class vs Gecko pass) is
the stable claim.
