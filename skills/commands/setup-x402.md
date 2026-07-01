---
description: "Wire x402 pay-per-call onto a provider's already-comprehended API via PayAI — point tools at the provider's OWN x402 endpoint, prove the handshake offline (X402_MODE=stub), keep the provider at 100%. Gecko never the rail, no cut. Usage: /setup-x402 <api>"
---

# /setup-x402

Wire x402 micropayments onto a provider's API via **PayAI**, offline-first. Run
`/make-agent-ready` first — this assumes the API is already comprehended and served.

**Argument:** the API (already agent-ready) whose priced operations you're wiring.

## Steps

1. **Map priced ops.** With the provider, decide which comprehended operations are
   priced vs free. Capture, per priced op, the amount (atomic units), the asset, and
   the provider's `payTo` address. These are the **provider's** values, not Gecko's.
2. **Wire x402 via PayAI.** Point the agent-facing tool at the provider's own
   x402-priced endpoint and surface the `402` challenge so the agent knows to pay.
   Confirm the PayAI facilitator URL/SDK against live docs; flag anything unverified
   `<!-- VERIFY -->`. Gecko hosts no payment endpoint. (See
   `skills/x402-payai-setup/wire-x402-payai.md`.)
3. **Verify offline.** Keep the default `X402_MODE=stub`. Run the full 402 → pay →
   200 shape with no spend and assert: 402 well-formed, `amount` in atomic units,
   `payTo` is the provider's, paid→200, no secret leaks. (See
   `skills/x402-payai-setup/verify-paid-call.md`.)
4. **Hand off for live (founder-gated).** Prepare the go-live command; the founder
   sets `X402_MODE` live and broadcasts. One live smoke, then stop. Claude never
   signs or broadcasts.

## Notes

- **Compose the rail, take no cut.** Money flows agent → provider, settled by PayAI.
  Gecko is never in the money path; the provider keeps 100%.
- **`payTo` is the provider's, never Gecko's.** If it ever isn't, that's a bug.
- **Default `X402_MODE=stub`; never flip to live without founder go-ahead.** Never
  sign or broadcast — founder-run only.
- **Offline-first (Pattern B).** The stub handshake is the deliverable and the
  debugger; live smoke is the final confirmation.
- **Status:** the handshake shape + offline verification are buildable today; live
  PayAI settlement is Building / founder-gated. Don't overclaim.
