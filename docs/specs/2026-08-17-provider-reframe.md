# The reframe: we were selling to the buyer we understood

**Status:** positioning correction, 2026-08-17. Supersedes the buyer half of every
positioning doc in this repo. The mechanisms do not change; who they are aimed at does.

---

## What was wrong

Every positioning artifact here is written at a **developer**. That is not an accident — it
is the pain we know, because we have felt it, and because the discovery interviews that
validated anything were with developers. The trouble is a mismatch we have known about for
months and never acted on:

> **Developers never pay. Revenue is provider-side.**

So we spent months sharpening arguments for the party we already understood and who will
never write a cheque, and never dug into the pain of the party who will. That is why the
positioning kept feeling hard. It was not underspecified; **it was pointed at the wrong
person.**

The correction is not a pivot. Everything built stays built, and it still helps developers —
it demonstrably does. But the *pain we lead with* has to be a provider's pain, and the
artifact we deliver has to be one a provider can act on.

## The provider's pain, measured

Not inferred. Every line below is something we observed on a real provider's live surface in
the past week, and **not one of them generated a support ticket.**

| what we found | what the provider knew |
|---|---|
| a compute figure reported **347× under** what the chain charges, across the whole catalogue | nothing — fixed within an hour of being told |
| **8 of 24** published flows require an input no agent can supply: green in their CI, uncallable in production | nothing |
| natural-language search returns *"No programs found"* for **5 of 5** real phrasings | nothing |
| an error code declared in the IDL and raised nowhere in the program — an agent reads it as a guarantee | nothing |
| the publish path runs fewer checks than the authoring loop | nothing |

**The pain is not "your documentation is bad."** It is:

> **Agents are already calling your API and failing, and you cannot see it.**

And the reason it is invisible is structural, not negligent:

* **A failing agent does not complain.** It does not file a ticket, it does not email
  support, it does not retry differently. It stops, and the developer who deployed it blames
  their own prompt.
* **It looks like ordinary traffic.** A wrong call is a 4xx among 4xxs. Nothing in a
  provider's dashboard separates "malformed request" from "an agent could not work out what
  this endpoint wanted."
* **Agent traffic is not even distinguishable.** Our own adoption telemetry found roughly
  **70% of the "clients" hitting a surface were MCP indexers** — a provider cannot separate
  an agent from a crawler, let alone a failing agent from a succeeding one.

A provider with a churn problem among agent builders has no instrument that would show it.
That is the gap, and it is the one thing we can hand them that nobody else does.

## What the score actually is

This closes the open question in the roadmap, and the answer is not the one I was
constructing.

**The score is a correctness rate, and the product is the delta.** Not "is this address
right" — that is an internal integrity check. What a provider buys is:

```
your surface, as an agent finds it today   ->  60%
your surface, comprehended                 ->  83%
                                               and here is the itemised what-changed
```

The frames.ag mechanic the founder pointed at is *"every outcome is quality rated."* Same
instinct, different unit: they rate an outcome, we rate a **surface** — and we can do it
before/after, which turns a rating into an argument.

**We already have this machinery.** `gecko/fcc_eval.py` plus `scripts/fcc_eval.py` runs
arms (raw spec dump versus Gecko comprehension), `N` times per task against a live cheap
model, and reports `fcc_rate`, `lift`, `hallucination_rate` and `per_archetype`, with the
headline decomposed into its component gates so a lift can be attributed rather than
asserted. It was built as an internal eval. **It is the provider artifact, mis-aimed.**

Three things it needs to become that:

1. **Run per provider surface**, not per committed golden set.
2. **Report what improved**, itemised, in a provider's own vocabulary — which endpoints, which
   intents, which failure mode.
3. **Keep the honesty note that is already in it:** this is the *comprehension* lift, not an
   accumulated-corpus lift, and a thin edge on a well-documented API is a real finding. A
   provider whose surface is already good should see a small number, and we should say so.

### What this does to the independent-witness problem

It demotes it from a headline risk to an integrity requirement, and that is a real
simplification. If the score is a correctness rate over many calls, a wrong-but-well-formed
address shows up as a **failed call** rather than as a per-account claim we have to defend in
public. But it must not show up as a *success* — a call that simulates clean against the
wrong account would inflate our own number. So the witness is still needed, and it is now
protecting the credibility of the lift rather than underwriting a claim about one address.

## Agent Cards — adopt, do not invent

The founder's instinct is right and the format already exists. **A2A's `AgentCard`** is a
JSON document at `/.well-known/agent-card.json` carrying identity, service endpoint,
capabilities, authentication schemes, and skills (id, name, description, inputModes,
outputModes, examples). A2A v1.0 is stable and governed under the Linux Foundation.

So: **`to_agent_card` is a projector over the graph**, alongside `to_fdl`. We do not invent a
format — we emit the standard one, and we emit it *derived* rather than hand-written, which
is the whole difference.

Two things make this more than a checkbox:

* **Most published Agent Cards are reportedly not actually A2A-conformant.** That is a
  scannable, publishable finding of exactly the shape as declared-vs-raised, and it is a
  reason for a provider to care before anyone asks them to.
* **A2A cannot express what our graph carries** — there is active literature on precisely
  what MCP, A2A and ACP *cannot* say. Derivation order, prerequisites, provenance per claim,
  value domains, arity: none of it has a field. Same relationship we have with FDL: emit the
  standard, carry the rest, and propose the missing fields upstream with a defect behind each
  one.

The developer build-kit sits on top: card plus runnable workflow plus rehearsal plus score.
The provider publishes it; the developer consumes it; only one of them pays.

## What changes in practice

* **Positioning documents get a new buyer.** The mechanisms, the proof, and the refusals all
  stay. The pain we lead with becomes the provider's, and the developer benefit becomes the
  *consequence* we can point at rather than the pitch.
* **The score becomes the deliverable**, and it is a delta, not a verdict.
* **`to_agent_card` joins the projector list.**
* **Discovery has a new subject.** We have never interviewed a provider about agent traffic.
  Every number in the table above came from measuring a surface, not from asking its owner —
  which is evidence of the pain and no evidence at all about whether they will pay to fix it.

## The one thing to be careful about

A before/after number is the most persuasive artifact we can produce and the easiest to
overstate. `60% -> 83%` is only honest if the baseline is a fair one: the provider's surface
as an agent genuinely finds it, not a strawman we built to lose. The existing harness already
takes this seriously — the baseline arm is the raw spec dump, which is what an agent
*actually* gets — and that discipline is the thing to protect when the number becomes a sales
artifact rather than an internal check.

**Sources:** [A2A agent discovery](https://a2a-protocol.org/latest/topics/agent-discovery/) ·
[A2A specification](https://a2a-protocol.org/latest/specification/) ·
[Most published Agent Cards are not actually A2A](https://apievangelist.com/2026/07/29/most-published-agent-cards-are-not-actually-a2a/) ·
[What MCP, A2A and ACP cannot express](https://arxiv.org/pdf/2606.31498) ·
[frames.ag](https://frames.ag/)
