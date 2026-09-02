# AGI for Gecko: an account-free, spend-capped credential minted over x402

Status: proposal for the founder's ruling. No code. Written 2026-09-01 after
the Apify comparison (agi.apify.com) and the blind-agent test.

## The hole this closes

A gated Gecko surface answers 401 with a self-serve mint path: `POST
/auth/login/start` with an email, then `/auth/login/verify` with the emailed
code. `auth.md` states the limitation itself: a fully headless agent needs
inbox access to finish. An agent with a wallet and no human can browse every
open surface and cannot open a gated one.

Apify closed the same hole with `agi.apify.com`: three endpoints, a 402
challenge over x402, and a prepaid API token whose balance is the spend cap.
The mint is off the request path once the token exists.

## What we would build

A mint at `agi.geckovision.tech` (or `/agi/*` on the MCP host; ruling
below), three endpoints, one document served identically as `/AGENTS.md`
and `/llms.txt`:

| Endpoint | What it does |
|---|---|
| `GET /protocols` | `{"protocols": [{"name": "x402", "url": "https://x402.org"}]}`. One protocol at launch. |
| `GET\|POST /protocols/x402/prepaid-tokens?amount=<usd>&currency=usd` | Without payment: 402 with the challenge. With a valid payment: `{token, remainingBalanceUsd, expiresAt}`. |
| `GET /prepaid-tokens/balance` | Bearer token in; `{remainingBalanceUsd, expiresAt}` out. |

The token is a Gecko key with a balance and a TTL. It goes through the
existing seam: `gecko.keyregistry` stores it, `gecko.keyauth` verifies it
on every gated mount, `gecko.authlogin` is untouched (the email path stays
for humans). One new field on the key record: `balance_micro_usd`, decremented
per gated call by a price the surface declares; a key at zero answers 402
again, naming the top-up endpoint.

## The 402 is a Gecko artifact

The 402 challenge carries `accepts[]` (network, asset, recipient, amount,
`maxTimeoutSeconds`) and an `extensions` block with the endpoint's own input
and output JSON Schema and one example response. An agent that reads the
challenge knows how to pay AND how to call, without a second document. That
is the comprehension output delivered through the payment handshake, which
is exactly the thing Gecko exists to produce. The challenge is generated
from the same OpenAPI description that `/openapi.json` serves; nothing is
hand-typed.

## Terms, stated as constraints an agent can read

- Minimum purchase: 1 USD. Maximum per token: 50 USD (cap the blast radius
  of a drained agent).
- Credit is non-refundable. The token expires 14 days after mint; unspent
  balance expires with it.
- A token opens the surfaces its price list names, per call, at the price
  the surface declares in its own entitlement data. No surface without a
  declared price is billable.
- Every debit is a ledger row: token id, surface, tool, price, remaining.
  Control plane only: no request bodies, no responses, no wallet addresses
  beyond the payment reference the facilitator returns.

## What "Gecko never holds a key" means here

Unchanged. The agent pays from its own wallet through its own signer (PayBox,
Phantom, Privy). Gecko receives a settlement reference from the x402
facilitator and mints a credential. A prepaid, capped credential is a
balance, not custody: Gecko holds no private key, signs nothing, and cannot
move the agent's funds. The one new thing Gecko holds is money already paid
to it, which is revenue, and the key record that proves it.

## How it composes with what exists

- `X402_MODE=stub` stays the default. In stub mode the 402 challenge is real
  and the settlement is the `FakeFacilitator` auto-grant, so the whole flow
  is falsifiable offline (Pattern B). `X402_MODE=live` needs the founder's
  explicit go-ahead, as today.
- The L1 fail-closed gate on gated mounts is the enforcement point. A
  prepaid key is one more key kind the gate already knows how to deny.
- `GATED_SURFACES` (today: birdeye) is the price list's domain. A surface
  becomes billable only when its provider config declares a price.
- The landing's `auth.md` gains a section "Pay instead of an inbox" and the
  PRM's `agent_auth` block gains the mint endpoint. `agents.md` gains one row
  in the integration table: "Agent with a wallet, no human, gated surface:
  buy a prepaid token".

## What we would NOT do

- No marketplace: the mint sells access to surfaces the operator already
  serves. Nothing is listed for sale on behalf of a provider.
- No custody: no wallet, no signing, no holding of anyone else's funds.
- No fabricated status: until settlement is live, the document says
  `"executable": false` with the email path as the fallback, the way Apify's
  auth.md does for its own preview.

## Open questions for the ruling

1. Host: `agi.geckovision.tech` (a separate front door, like Apify) or
   `/agi/*` on `mcp.geckovision.tech` (one host, one WAF, one deploy)?
   Recommendation: the MCP host; a subdomain is a second deploy to keep
   honest for one page of endpoints.
2. Facilitator: PayAI (already in `x402_pay.py`) or pay.sh? Recommendation:
   PayAI in stub first; the live choice waits for the provider pricing
   ruling.
3. Pricing source: per-surface flat price in the provider config, or per
   tool? Recommendation: per surface, one number, until a provider asks for
   more.
4. Does this change the "developers never pay" principle? A developer's
   agent paying for a gated third-party surface pays the surface's price;
   Gecko's cut is zero by the standing rule. Whether Gecko charges the
   provider for the rail is the pricing ruling, not this spec.

## Cost to build

Stub-mode end to end: two to three days. Three route handlers on the host,
one key kind with a balance, a price field in the provider config, the 402
generator from the OpenAPI description, the AGENTS.md document, tests
against the FakeFacilitator. Live settlement: the founder's go-ahead plus
the facilitator's real keys, no new code path.
