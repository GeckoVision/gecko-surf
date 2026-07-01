# Step 3 — Verify the paid call offline, before any live settlement

**Status: Building (Pattern B).** gecko-surf has no x402 handshake yet — the offline
stub verification at `X402_MODE=stub` is the simulation you build first, before any
live settlement.

The rule for every wire integration in Gecko: **the first deliverable is a free
offline simulation that can falsify the implementation. Live smoke is the final
check, never the debugger.** x402 is a wire integration, so it follows the rule.

## Why offline-first for payments especially

A live payment bug costs real money and is slow to iterate on. A stubbed handshake
is free, deterministic, and offline — you can run it hundreds of times while you get
the shape right. By the time you touch the live rail, the only thing left to verify
is that the wire behaves like the stub — not whether your logic is correct.

## The stub handshake

Keep the default `X402_MODE=stub`. In stub mode the priced call is built to run the
**full 402 → pay → 200 shape** without moving money:

- The priced op returns the `402` challenge (asset, amount, `payTo`, facilitator).
- The agent builds an `X-PAYMENT` payload for that challenge.
- The **stub** facilitator "verifies" and returns success — **no Solana settlement,
  no spend.**
- The op returns `200` + a schema-correct example (from `recorded` mode).

This proves everything that can be wrong in the *shape*: is the priced op mapped,
is the challenge well-formed, is `amount` in atomic units, is `payTo` the provider's
address, does the agent attach the header, does the tool re-send correctly.

## What to assert offline

- **Unpaid call → 402** with a well-formed challenge (all required fields present).
- **`amount` units** match the asset's decimals — the silent, load-bearing one.
- **`payTo`** is the **provider's** wallet, never Gecko's. (Gecko is never in the
  money path — if `payTo` is ever a Gecko address, that's a bug.)
- **Paid call (stub) → 200** + the expected response shape.
- **No secret leaks** — the payment payload and any keys never appear in tool defs,
  logs, or error messages; redact before raising.

Because `recorded`/stub and `live` share one code path and differ only at the
transport edge, a paid call that shapes up in stub is the same call live — the
comprehension is identical.

## Going live (founder-gated)

Only after the stub handshake passes:

1. **Founder go-ahead required** to set `X402_MODE` to live. Never flip it during
   user-testing or on your own.
2. **Founder-run settlement only.** Claude prepares the exact command; the founder
   broadcasts the mainnet transaction. Claude never signs or broadcasts.
3. **One live smoke, then stop.** Confirm the wire matches the stub on a single real
   call — it's a confirmation, not a debugging loop. If it diverges, go back to the
   stub to diagnose; don't brute-force against the live rail.

## The boundary (again, because it's money)

The provider keeps 100%. Gecko composes PayAI and takes **no cut**, holds no funds,
signs nothing. If verification ever shows money routing through Gecko or a take-rate
appearing, that's not a config issue — it's out of lane. Stop and re-read
[`rules/aggregate-not-rail.md`](../../rules/aggregate-not-rail.md).

Back to the spine: [SKILL.md](SKILL.md).
