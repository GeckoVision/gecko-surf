# Gecko positioning — "Stop guessing"

**Status:** canonical (2026-07-26; extended 2026-08-13). Supersedes the old grunt line
*"Any API, agent-ready — first call correct."* Every outward surface — README, deck,
landing, video, tweet — draws its message from this file.

---

## The 2026-08-13 layer — the stack, founder-chosen

The enemy line below is unchanged. Two pieces sit with it now, chosen after agents
started spending real money through the engine (fifteen mainnet transactions, fifteen
exact compute predictions, the last four signed in an enclave with no key on the
planning machine):

> **Tagline:** Check the call before it counts.

> **The formula statement:** We help developers whose agents guess their way through
> APIs turn every surface into a call graph the agent checks instead of guesses from —
> so the first call is the right call, **even when it spends**.

**Why the statement subordinates spending to calling — and must keep doing so.** The
rejected variant led with the wallet moment ("the agent buys and pays on its own"),
which invites the question *"so you run a wallet? are you a payment solution?"* — the
one identity confusion we can never afford: APIs get PAID is Metera/MCPay's row; ours is
APIs get USED. "Even when it spends" frames the spend as the *stress test* of
comprehension, never the offering.

**The prepared answer when someone asks anyway** ("are you a wallet / a rail?"):

> Neither — deliberately. Gecko holds no key, moves no money, and takes no cut. The
> wallet is yours — we compose with Privy, Phantom, whatever signs for you. The rail is
> x402's. What we own is the step everyone skips: *is this call, this transaction, the
> right one?* We turn the API's surface into a call graph, verify the exact bytes
> against a simulation, and hand your wallet something checked. Fifteen mainnet
> transactions, fifteen exact cost predictions — and in every one, the key was never
> ours.

**The 30-second pitch** (spoken, ~80 words — pause after the first line, stop after the
last number):

> Every call your agent makes is a guess — it reads a schema, picks an endpoint, and
> hopes. Survivable, until agents started spending real money.
>
> Gecko turns the API's surface into a call graph the agent checks instead of guesses
> from. And for anything that spends, it verifies the exact bytes against a simulation
> before your wallet ever sees them.
>
> We hold no key and move no money — we're the check before it counts. Fifteen mainnet
> transactions. Fifteen exact cost predictions.

**Words that never enter the sentence:** "safe/safety", "easily". Every agent company
says safe; nobody else says *what the receipt predicted is what the chain charged*. The
hard-to-fake claim beats the adjective.

---

## The line

> **Stop letting your agent guess.**

Sub-line (the explanation, for anyone who already has an OpenAPI spec):

> Your OpenAPI spec is a **schema** — a description your agent *guesses* from.
> Gecko turns it into the **call graph** it doesn't have to guess from.

That contrast — **schema → call graph** — is the load-bearing idea. Lead with the
imperative ("stop guessing"); explain with the contrast.

## Why the old line underperformed

- **"Any API"** is a capability claim. A developer already shipping with APIs hears it and
  thinks *"I don't need any API — I need MY APIs, and they work."* It invites the objection
  instead of answering it.
- **"First call correct"** is a metric for a problem they don't believe they have
  (*"my calls work fine"*), and it reads as a one-time nicety, not a reason to change.

Both described **our feature**, not the developer's **worldview**. The reframe attacks the
worldview: *every call your agent makes is a guess.*

## The thesis — probabilism × determinism

An LLM is **probabilistic**. Handing it a schema (OpenAPI / GraphQL / MCP) and asking it to
pick the right endpoint, the right order, the right parameters, and whether to trust the
response is asking it to **guess** — and agents hallucinate at *every* stage: perception,
tool selection, parameter generation, response handling.

You cannot make the model deterministic.

> **So make the *surface* deterministic.**

Gecko infers the **deterministic call graph** from the spec — the exact sequence, the joins,
the provenance — and hands *that* to the model. Structure over guessing. It is the same move
Graphify made against RAG ("structure, not similarity"); we make it against OpenAPI/MCP
("determinism, not guessing").

## The objection, answered (the settled-user journey)

> *"I already use many APIs and my agent isn't struggling. Why add a tool?"*

1. **You can't *see* it struggling.** You check the final answer — not the wrong calls it
   made and retried to get there. End-to-end success hides intermediate hallucinations; your
   real first-call rate is lower than you measure. Gecko makes the guessing — and your true
   failure rate — **visible**.
2. **"Fine" is survivorship.** It works on the handful of APIs you hand-wired and babysat.
   It breaks **silently** when the spec drifts, when you add the next painful/paywalled API
   your agent *doesn't* one-shot, and it taxes you on every retry (latency, tokens, money).
   And a poisoned spec or image walks right in — you would never see that either
   (Skill Guard).
3. **The thing you can't do by hand: correlate.** Chain one API's output into another's
   input, across providers, with provenance. No schema gives you that. *(This is the
   Video-2 / V2 message — keep it out of the top-line pitch until it ships.)*

So the real question isn't *"does my agent work"* — it's *"how would I even know it's
guessing, and what happens on the next API, the next drift, the next attack?"*

## What survives as proof (not headline)

- first-call-correct on painful/paywalled APIs
- −99.5% false correlation links on Stripe; perfect inference on TaskBench
- Skill Guard catching image-borne injection others miss
Use these as **evidence for the determinism claim**, never as the claim itself.

## The competitive frame (schema vs. call graph)

| | OpenAPI | GraphQL | MCP | **Gecko** |
|---|---|---|---|---|
| Provides | schema | schema | tools | **the call graph** |
| Which call, in what order | guess | guess | guess | **derived** |
| Provenance (why this call) | — | — | — | **✓** |
| Safe under a poisoned surface | — | — | — | **✓ (Skill Guard)** |

The incumbents describe *what exists*. Gecko derives *what to do* — deterministically.

## Voice rules

- Lead with the pain and the imperative ("stop guessing"), not the capability.
- Name the enemy: **guessing** (the probabilistic tool call), not "wrong APIs".
- Reframe "any API" → *"the call your agent gets wrong when you're not looking"* (the
  painful-Nth-API ICP), never "any API."
- Anti-poisoning is a **feature of comprehension**, not a separate firewall product.
- Correlation is the V2 frontier — a **separate** message (Video 2), not the top line.
- No over-claim: measured numbers only; the close is the positioning, not a promise.
