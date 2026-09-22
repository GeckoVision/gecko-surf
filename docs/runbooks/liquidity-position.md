# A liquidity position on mainnet, opened and funded through Gecko

One Orca Whirlpool position on the USDG/USDC pool (`9RqDTfwC…`, tick spacing 1), signed
by a PayBox wallet, every transaction simulated, bound and gated before the wallet signs.
`scripts/open_position.py` is the runner; `gecko/providers/whirlpool_position.py` plans it.

## What it does

1. **Open.** `open_position` (classic SPL position mint). A fresh position-mint key signs
   its own slot once and is dropped; the position PDA owns the mint afterwards, so the key
   controls nothing. The wallet pays the rent (about 0.0065 SOL, refunded on close) and the
   fee. A fee relay cannot carry this: it caps 0.001 SOL per transaction, and the authority
   role refuses any transaction that moves the authority's SOL.
2. **Deposit.** `increase_liquidity_by_token_amounts_v2`. The wallet names two maxima; the
   program sizes the liquidity and reverts outside the sqrt-price bounds (default ±1%).
   No liquidity arithmetic of ours is on the money path.

Each leg has its own spend policy: one Whirlpool instruction plus ComputeBudget, the
writable accounts derived here (never the builder's echo of its own work), the rent bound on
the open, and per-mint caps at exactly the maxima on the deposit. USDG is measured under the
operator's `AcceptedMint` (`scripts/operator_policy.py`), the same pin the route uses.

PayBox signs as the wallet's own fee payer only because this runner opens the backend with
`self_paid=True`; every other runner keeps the authority-only scope. The backend refuses to
pay for bytes that name another fee payer, and refuses the authority role when the bytes
make the wallet the fee payer.

```bash
uv run python scripts/open_position.py --network mainnet --rpc-url "$RPC_URL" \
  --signer paybox --token-max-a 5000 --token-max-b 5000                     # dry run
# --broadcast to open and fund, founder-authorized
# --position-mint <mint> to fund a position already opened (never opens a second one)
```

The dry run plans both legs, simulates the open and runs the open's gate. With
`--position-mint` it simulates and gates the deposit instead.

## Measured 2026-09-22

Security review before the run (defi-security lane): land with changes; six findings
applied. Then, founder-authorized in chat:

| transaction | signature | CU predicted / charged |
|---|---|---|
| open, range [-1, 2) | `64EsSSNXTzHythqv6UZBQrg2zaoeRc5E79YQauGviBz3ikAHaFXHWmycUKdeWaLwERZmfnN1ahaPyKEVvUZ84mtX` | 44,171 / 44,171 |
| deposit 0.005 USDG + 0.003908 USDC | `4LZAK7EvvhjUVdTrxnudTtE288HTW9pf8c1rTC2XJtx1F8XZEbn4GQy4UWJ8Ev5rRTZVB7LJPmqS5nxP6mXT83Wy` | 24,396 / 24,396 |

Position `EzQ5YvU3kpppJjQX7xaMtB6XvEjsVrnUyf4aJgmvRZen`, mint
`8y7umbigibpwBYSLYXYjkxYw4syoXcNRAoNLAH8gBnF5`.

The first deposit attempt was refused at simulation, before any signature: the remote
builder encoded the `IncreaseLiquidityMethod` enum's variant tag and dropped its four
fields (9 bytes of 58), and the program answered `BorshIoError` at 734 CU. Gecko now
completes a truncation from the IDL (`complete_increase_data`) and refuses any other
difference. The builder bug is reported to its maintainer.
