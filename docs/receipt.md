# The Receipt — what it asserts, and what it does NOT

A **Receipt** is the output of Gecko's `simulate` engine (`gecko/simulate.py`): it closes a
built transaction into one legible, honest verdict *before any money moves*.

## What a Receipt asserts

- **Would this transaction LAND** against a *snapshot* of on-chain state (`status`:
  `pass` / `fail`), via `simulateTransaction` (`sigVerify:false`,
  `replaceRecentBlockhash:true`, `commitment:"processed"`).
- **Why it wouldn't**, as a **categorical `revert_class`** — a stable string vocabulary:
  `slippage`, `custom_program_error:<code>`, `insufficient_funds`, `account_error`,
  `other`. Never a fabricated dollar number.
- **Compute units** consumed (`units_consumed`).
- **Best-effort deltas**: `sol_delta` (post − pre lamports for the first tracked account)
  and `tokens_received` (only when a token account is tracked and decodable — otherwise
  `None`, never guessed).
- A **`network_label`** honesty caveat, present on every Receipt.

## What a Receipt does NOT do

- It does **not predict price or slippage** — only whether the tx lands against a snapshot.
- A fork/RPC result is **NOT mainnet** — the `network_label` says so; a surfpool-fork
  Receipt is never presented as mainnet truth.
- It says **nothing about send-time blockhash validity** — the simulation uses
  `replaceRecentBlockhash:true`, so the real transaction must take a fresh blockhash at
  sign time (the builder/lander's job); a passing Receipt can still expire before it is
  signed.
- It does **not quote a priority fee** — `SetComputeUnitPrice` defaults to 0 in the
  simulated bundle; landing under mainnet load needs a fee the operator (or builder)
  supplies. The Receipt's `units_consumed` is the honest input for the CU *limit*, not
  the price.
- It **stores nothing** — the Receipt is returned to the caller and persisted nowhere
  (control-plane invariant #1). No payload, pubkey, or log line is written.
- It **never signs and never broadcasts** — `simulateTransaction` only. No keypair, no
  `sendTransaction`. Mainnet broadcast stays a separate, founder-signed step.

## Path A vs Path B

**Path A (Gecko runs it):** hand a built plan (with `fee_recipient` supplied) to the
`simulate` MCP tool on the program surface; it builds, simulates, and returns the Receipt.
One tool call, no glue code.

**Path B (self-serve):** `plan_buy` returns a `simulate` recipe block — fill
`fee_recipient`, POST `build_url` to get the tx, then run `simulateTransaction` yourself.
The dev owns the loop; Gecko supplies the correct account set + the recipe.

Either path: `fee_recipient` stays an honest gap — Gecko will not guess it; the caller
supplies it.
