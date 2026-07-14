# x402 go-live runbook — flipping `X402_MODE=live`

Operational, founder-run. The repo ships `X402_MODE=stub` (FakeFacilitator, no real
USDC) and that stays the default; `live` is flipped **only via the hosted deploy env**,
only with founder go-ahead. Claude never signs, broadcasts, or runs a live settlement —
every step below that moves real value is **founder-run**.

All values below are placeholders. Real ones live in the deploy secret store — never in
this repo, never in `.env.example`.

## 1. Env to set on the hosted deploy

| Var | Required | Meaning |
|---|---|---|
| `X402_MODE` | flip switch | `live` to enable; unset/`stub` = FakeFacilitator |
| `X402_FACILITATOR_URL` | yes (live) | facilitator base; client POSTs `{url}/verify` + `{url}/settle` |
| `X402_FACILITATOR_TOKEN` | no | bearer for the facilitator, if it needs one |
| `X402_PAY_TO` | yes (live) | Gecko treasury address that receives the USDC |
| `X402_ASSET` | yes (live) | USDC mint / asset id the plan settles in |
| `X402_NETWORK` | yes (live) | e.g. `solana-mainnet` — injected, never hardcoded |

Fail-fast behavior (test-backed): with `X402_MODE=live` and any required var missing,
mode resolution raises `X402ConfigError` naming exactly the missing vars — the deploy
fails at startup, not at the first paying customer. A private/loopback/non-http
facilitator URL is rejected (`UnsafeUrlError`) before any socket opens.

```bash
# Deploy env (placeholders — real values from the secret store)
X402_MODE=live
X402_FACILITATOR_URL=https://facilitator.example.com
X402_FACILITATOR_TOKEN=<optional-bearer>
X402_PAY_TO=<TREASURY_PUBKEY>
X402_ASSET=<USDC_MINT>
X402_NETWORK=solana-mainnet
```

## 2. Founder-run smoke sequence

Run the three stages in order; do not skip to 3.

**Stage A — stub rehearsal ($0, offline).** With `X402_MODE` unset/`stub`, drive the
full subscribe path once (the recorded/e2e suite is the same call path):

```bash
uv run pytest tests/test_x402_pay.py tests/test_x402_facilitator.py -q
```

Expected: all green — envelope minted, challenge validated, entitlement granted through
`FakeFacilitator`. This proves the path; live only swaps the adapter.

**Stage B — live verify-only (no funds move).** Set the full live env on a staging
shell, construct the client, and relay a single `verify` with a real wallet-signed
X-PAYMENT payload against the served requirements. `verify` never settles — the x402
`/verify` endpoint moves no funds. Expected: `True` (or `False` with the facilitator's
reason). Any transport doubt raises `FacilitatorError` — that's the fail-closed design,
not a bug to route around.

**Stage C — one real settle, tiny amount.** Configure a throwaway plan with the
smallest sane price (e.g. `price=10_000` = 0.01 USDC), run one full
`settle_subscription` with a real payment. Expected: a `Settlement` whose reference is
the facilitator's transaction id, and one `cloud` entitlement with the correct
`expires_at`. Confirm the transaction on-chain (explorer) and the USDC in the treasury.
Then restore the real plan price.

## 3. What to watch in logs

- `X402ConfigError` at startup → the env is incomplete; the message names the vars.
- `FacilitatorError` → transport/protocol failure or facilitator refusal. Message
  carries endpoint + HTTP status + a short scrubbed reason only. A burst of these on
  `/settle` means paying customers are being (correctly) denied — page the founder.
- `ChallengeError` → a 402 body failed the pinned policy (payment-swap defense) or a
  payment didn't authorize the served terms. Expected on tampering, never on the happy
  path.
- Redaction check: no log line ever contains the bearer token or an X-PAYMENT payload.
  If one does, that's a sev-1 — rotate the token and fix before continuing.
- Idempotency: a replayed payment must NOT produce a second `/settle` call (the
  pre-settle short-circuit) or a second grant.

## 4. Rollback

Unset `X402_MODE` (or set `stub`) on the deploy and redeploy — mode resolution falls
back to `FakeFacilitator` immediately. Already-granted entitlements are control-plane
records and keep working until their `expires_at`; no funds are touched by a rollback.
The facilitator env vars can stay set — stub mode never reads them (test-backed).

## Hard boundary (restating the repo rule)

Gecko's server — and Claude — signs nothing and broadcasts nothing, in every mode. The
customer wallet signs; the facilitator settles; Gecko relays payloads and stores only
`{entitlement, expires_at, opaque payment_ref}`.
