# Step 2 — Wire x402 on the provider's API via PayAI

**Status: Building / founder-gated.** The engine has the auth/session seam; live
x402 settlement is not wired in `gecko-surf` yet and is founder-gated. Build and
prove the handshake **offline first** ([verify-paid-call.md](verify-paid-call.md)).

> **Verify before shipping.** PayAI-specific endpoints, the facilitator base URL,
> and SDK call names below are marked `<!-- VERIFY -->`. Confirm each against
> PayAI's live docs — do **not** ship an invented URL or method name.

## The x402 handshake (what a paid call looks like)

x402 is HTTP-native. One priced operation, one round of challenge-then-pay:

```
1. Agent → provider:   GET /v1/priced-op                (no payment)
2. provider → agent:   402 Payment Required
                       {
                         "accepts": [{
                           "scheme": "exact",
                           "network": "solana",
                           "asset": "<USDC-mint>",
                           "amount": "1000",             # atomic units — load-bearing
                           "payTo": "<provider-wallet>",
                           "facilitator": "<PayAI facilitator URL>"   <!-- VERIFY -->
                         }]
                       }
3. Agent:              build a payment for that challenge (PayAI client/SDK)   <!-- VERIFY -->
4. Agent → provider:   GET /v1/priced-op
                       X-PAYMENT: <base64 payment payload>
5. provider ⇄ PayAI:   facilitator verifies + settles on Solana
6. provider → agent:   200 OK + the data  (+ X-PAYMENT-RESPONSE receipt)
```

The money moves **agent → provider**, settled by **PayAI**. Gecko is **not** in the
money path. The provider keeps 100%.

## Where Gecko fits (and where it does not)

Gecko's contribution is **comprehension of the paid surface**, nothing more:

- **Point the tool at the provider's own x402 endpoint.** The agent-facing tool
  Gecko generated already targets the provider's URL; for a priced op it targets the
  provider's **x402-priced** URL. Gecko does not host a payment endpoint.
- **Surface the 402 as access metadata.** The comprehension marks a priced op so the
  agent knows the call requires payment — the same "access & auth" seam that hides
  API keys (`Session.auth_headers()`), extended to "this op speaks x402."
- **Never custody, sign, or broadcast.** The `X-PAYMENT` payload is built by the
  agent's PayAI client against the provider's `payTo`. Gecko does not hold keys, does
  not sign, does not settle.

`amount` is in **atomic units** — the same load-bearing detail as anywhere else in
comprehension. A wrong-unit payment *succeeds* for the wrong amount. Confirm the
asset's decimals; the challenge's `amount` is authoritative.

## Wiring steps (offline-first)

1. **Confirm the provider's x402 endpoint** for each priced op (from step 1's map):
   the priced URL, `payTo`, asset, and amount. These are the **provider's** values.
2. **Confirm the PayAI facilitator** the provider settles through:
   `<PayAI facilitator URL>` and the client/SDK the agent uses to build the payment.
   `<!-- VERIFY against PayAI docs -->`
3. **Keep `X402_MODE=stub` (the default).** Do not set live. Build the whole
   handshake against the stub and prove the paid-call shape offline
   ([verify-paid-call.md](verify-paid-call.md)).
4. **Live smoke is the final check, never the debugger** (Pattern B). A real
   mainnet settlement is **founder-run only**: Claude prepares the command and hands
   it over; the founder broadcasts. Never flip `X402_MODE` to live without explicit
   founder go-ahead.

## The boundary

Composing the rail is the whole job — **take no cut, hold no funds, own no
marketplace.** If a wiring step routes money through Gecko, adds a take-rate, or
turns the priced surface into a hosted catalog, stop and re-read
[`rules/aggregate-not-rail.md`](../../rules/aggregate-not-rail.md).

Next: [verify-paid-call.md](verify-paid-call.md) — prove it offline before any live
settlement.
