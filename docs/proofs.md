# Live proofs — the E2E side-by-sides, verbatim

The two landing-orchestrator numbers the architecture cites (pump `86,669 CU`,
Meteora `81,964 CU`) come from real env-gated E2E runs. This file is their durable
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

## Recording an outcome from a re-run (opt-in)

Either orchestrator (and the Path-A `simulate` MCP tool) can append the run's
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
