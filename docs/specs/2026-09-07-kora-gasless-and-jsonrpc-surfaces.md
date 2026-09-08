# Kora, gasless purchase, and the JSON-RPC surfaces OpenAPI cannot describe

**Date:** 2026-09-07 · **Status:** measured findings, two builds proposed and unapproved ·
**Feeds:** `2026-09-07-onboarding-catalog-and-autonomous-agents.md`, the weekly build-in-public video

**Boundary, stated first.** Nothing in this document proposes that Gecko hold a key, sign,
or broadcast. Kora holds its own fee-payer key and signs with it; Gecko comprehends Kora's
surface, builds unsigned bytes, binds a receipt to those bytes, and refuses what its policy
rejects. The mainnet run at the end is founder-run, as every mainnet run in this repository
is. Stage 2 touches the signing path and must not merge without a security pass.

## 1. What was measured, and how to reproduce it

The founder asked for a demo in which an agent holding only USDG, with no SOL, buys a
coffee: Orca Whirlpool for the swap, Kora for a gasless transaction. Four measurements
were taken on 2026-09-07 before any code was written.

| # | Measurement | Value | How to reproduce |
|---|---|---|---|
| 1 | Kora's shipped OpenAPI | 30 schemas, **0 paths** | `gecko.ingest.extract_operations` over `kora/openapi.json` returns 0 operations |
| 2 | Kora's OpenAPI, regenerated with their own documented command | 30 schemas, **0 paths** | `cargo run -p kora-cli --bin kora --features docs -- openapi` (the `just openapi` recipe) |
| 3 | RPC methods Kora actually serves | **12** | `build_docs_spec()` in `crates/lib/src/rpc_server/rpc.rs`; 10 toggled in `kora.toml` |
| 4 | SOL the buyer paid in the existing mainnet coffee chain | 5,000 lamports, all five transactions | `docs/mainnet-swap-chain.md` |

Measurement 2 matters more than 1. The committed file is not stale: their own generator,
run today, emits a spec with no operations. An agent that finds Kora and reads its
specification learns zero ways to call it, and Kora is a Solana Foundation project, so this
is the norm rather than an outlier.

Measurement 4 is the honest statement of the gap. The USDG-only chain already closed end to
end on mainnet, but the buyer paid the network fee in SOL every time. "No SOL" is not yet
true of anything we have shipped.

## 2. Why the spec is empty, and why that generalizes

Kora's wire, read from `crates/lib/src/rpc_server/server.rs`:

- `GET /liveness` is proxied to the `liveness` method by `ProxyGetRequestLayer`. It is the
  only route with a path of its own.
- Every other method is `POST /` carrying a JSON-RPC 2.0 envelope, on port 8080 by default.

OpenAPI describes operations as (path, HTTP verb) pairs. Twelve methods sharing one `POST /`
cannot be twelve operations. The generator had all twelve method schemas available and
emitted none of them as paths, because there is no correct way to emit them. This is not a
Kora defect. It is what happens to every JSON-RPC API described with a REST description
language, which on Solana is most of the infrastructure an agent would want to reach.

**The consequence for Gecko's positioning.** The comprehension layer is not "we read your
OpenAPI better than you wrote it". For this whole class of surface there is no OpenAPI worth
reading, and the surface still has to become callable. That is a stronger claim than the one
we usually make, and it is measurable per API.

## 3. Prior art in this repository: the Jito boundary decides the gating question

`gecko/jito_surface.py` already settles the question a Kora mount raises, for a
structurally identical service. Jito's Block Engine is mainnet-money infrastructure reached
over JSON-RPC. Its ops split on a founder-confirmed line, quoted from that module:

> READ ops are served LIVE (public, keyless, no signing, no spend). WRITE ops are kept
> RECORDED (money-movers we CATALOG but must NEVER relay). Serving these live would turn
> our public endpoint into an open MEV relay, a control-plane violation. The agent takes
> Gecko's first-call-correct comprehension and submits these DIRECTLY to Jito with its OWN
> wallet. We are the catalog, not the relay.

Kora is the same shape with the stakes moved one step closer: Kora holds a funded fee-payer
key, so a Kora-backed signing tool on the public mount would let any anonymous caller spend
our SOL. The same line applies, and no new ruling is needed.

| Kora method | Lane on our mount | Why |
|---|---|---|
| `liveness`, `getVersion`, `getConfig`, `getSupportedTokens`, `getBlockhash`, `getPayerSigner` | live | reads, no signing, no spend |
| `estimateTransactionFee`, `estimateBundleFee` | live | a simulation and a quote; this is the number the demo puts on screen |
| `signTransaction`, `signAndSendTransaction`, `signBundle`, `signAndSendBundle`, `transferTransaction` | catalog only | each one spends the fee payer's SOL; the agent calls Kora directly with its own credentials |

`jito_surface.py` exists as one module precisely so a money boundary cannot drift between
the two hosts that serve it. `kora_surface.py` must be shaped the same way for the same
reason.

## 4. Stage 1: make Kora callable, and serve it

No signing-path code changes. This stage is reviewable on its own and is what makes the
founder's "run it on our MCP" requirement true.

- `gecko/kora_surface.py`, owning the lane table above, on the `jito_surface.py` model.
- Twelve tool definitions built from the request and response structs in
  `crates/lib/src/rpc_server/method/*.rs`, which carry doc comments good enough to become
  tool descriptions. Provenance is `recovered from source`, never `declared`, because the
  published specification does not contain them.
- Transport over `gecko/rpc.py`, the canonical JSON-RPC transport, rather than the OpenAPI
  caller. An OpenAPI spec with twelve virtual `/{method}` paths must NOT be written: the
  Jito module records that exact mistake sending `POST /getTipAccounts` and 404ing every
  read.
- The base URL is pinned, as every Gecko surface pins one, because the pin is the trust
  anchor the auth guard rests on.
- Mounted gated, not on the public shelf, until stage 2 is reviewed.

## 5. Stage 2: the fee payer that is not the buyer

This is the signing-path change, and it is the whole of the "no SOL" claim.

**What exists.** `gecko/landing.py` already takes a `fee_payer` argument (lines 328 and 496)
and `gecko/txbind.py` already models the fee payer as a field of the bound message (line
298), reading it from the message's first key. So a distinct fee payer is representable and
it is covered by the binding.

**What blocks it.** `gecko/prepare_purchase.py` hardcodes the fee payer to the buyer in two
places (lines 767 and 872). The purchase path has no way to express "somebody else pays the
network fee".

**The shape of the change.**

1. Thread an optional fee payer through the purchase and swap planning paths, defaulting to
   the buyer so every existing caller is unaffected.
2. Keep the buyer resolution exactly as it is. `_resolve_buyer` looks the buyer up and
   refuses a supplied mismatch (`buyer-not-bound`), and a fee payer is a different concept
   from a buyer. A change that lets a fee payer weaken buyer resolution is the failure mode
   to design against, and it needs a refusing test of its own.
3. Kora joins the signer directory in `gecko/prepare_purchase.py`, on the pattern of the
   Orquestra signer entry, with one property proven before the entry is added: Kora signs
   the exact bytes it was handed. `signTransaction` returns `signed_transaction` and
   `signer_pubkey`, and the flow is Kora signs, the buyer signs, the client sends, which is
   what Kora's own lighthouse note requires.
4. `gecko/effects.py` should read the same way it always has, with the SOL delta landing on
   the fee payer and the buyer's SOL unchanged. That is the receipt line the demo needs, and
   it must be derived rather than asserted.

**Open question for the founder.** Kora's default config allows neither Orca Whirlpool nor
the store program, and its `allowed_tokens` list is USDC only, with `price_source = "Mock"`.
Running this demo means widening that allowlist and pointing it at a real price source. That
allowlist is Kora's own spend policy, so widening it is a deliberate act with a cost, and it
belongs next to `gecko/spend_policy.py` in the record as the twin the
`2026-08-12-autonomous-signing-blockers` spec describes.

## 6. Applicability, since the coffee is a showcase and not a reason

Four claims, in the order they hold up.

1. **No agent wallet is born ready.** An agent is funded in one asset, the thing it must buy
   is priced in another, and the chain charges gas in a third. A person fixes that by hand.
   An agent that must ask a person to top up its gas is not autonomous, and that is the
   default state of every wallet an engineer did not prepare.
2. **Gas fails a run at the worst moment.** It fails last, after the swap already spent
   tokens, leaving the wallet in a state nobody chose.
3. **The payer is not the actor, and that is how organisations work.** The company pays, the
   employee acts. Kora is that separation on Solana: fund N agents without giving any of
   them SOL, revoke by defunding one address, and cap what every agent may cost in one
   place. That is what a finance function wants before it lets agents transact.
4. **Nothing documents the join.** No page anywhere says "if the buyer has no SOL, obtain a
   fee payer from a relayer and route the swap through the pool derived from the mint pair".
   A person assembles it once per combination and again when a mint or a pool changes. On
   transaction 1 of the existing chain, four well-formed wrong answers were caught by
   comparison rather than by inspection, including a wrong pool derived from a wrong fee
   tier. Each was a real, valid, wrong transaction.

## 7. What this does not claim

- No gasless transaction has been built or landed. Kora appears nowhere in `gecko/` today.
- The Kora binary was built from the clone at `~/PycharmProjects/Gecko/kora` and its spec
  regenerated. No Kora node has been run, configured, or funded.
- Willingness to pay is unvalidated, here as everywhere else in this repository.

## 8. Order of work

1. Stage 1, gated mount, reviewable alone.
2. A security pass on stage 2's design before it is written, per the signing-path rule.
3. Stage 2 behind the default-to-buyer flag, with the refusing test from section 5.
4. A simulation the founder reads, then a founder-run mainnet transaction. The founder's
   instruction for the video is that a simulation may be shown only when a real mainnet
   transaction follows it.
