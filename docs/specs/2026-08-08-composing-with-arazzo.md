# Composing with Arazzo — what the standard carries, and what we add

**2026-08-08.** Evidence-first. Every claim below was run, not reasoned about.

## What Arazzo is

A community specification under the OpenAPI Initiative for expressing **sequences of
calls and the dependencies between them**. It is the right format, it is well designed,
and it is the one an agent ecosystem should converge on. We emit it today
(`gecko export-arazzo`) and we validate against the official schema rather than against
assertions we wrote.

We should say plainly: **Arazzo is not a competitor and not a gap in our product.** It is
the artifact our derivation produces. If it succeeds, we benefit.

## What we ran

| check | result |
|---|---|
| our vendored schema vs `OAI/Arazzo-Specification@main` | byte-identical |
| all 5 official examples against our vendored copy | 5/5 valid — 9 workflows, 18 steps |
| our derived cross-API plan through **Redocly CLI** (third party) | `Woohoo! Your API description is valid. 🎉` |
| our **refused** plan through the same linter | `❌ Validation failed with 1 error` · exit 1 |

That last pair is the important one, and it is worth being precise about why.

## The three things we bring

### 1. Derivation across surfaces, not within one

The generators in the Arazzo ecosystem analyse **a single OpenAPI description** and find
logical sequences inside it. That is genuinely useful and it is not what we do.

Here is the striking part, and it is an argument *for* Arazzo, not against it. **The
specification's flagship illustration is a two-API workflow.** `Arazzo-PetAdoption-
Workflow.gif` and the structure diagram both show `FindAndAdoptPet` spanning `petsAPI`
and `adoptionsAPI` — two separate `swagger.json` URLs. Crossing APIs is not an edge case
they tolerate; it is the picture they lead with.

And that example **exists nowhere in the repository as text.** No YAML, no markdown —
`grep -rl adoptionsAPI` returns nothing. Every one of the five committed examples has
exactly **one** `sourceDescription`.

So the standard is pictured doing the thing nobody has automated. Our export carries
**two** sources — Pegana and Birdeye — because the join between them is in neither spec.
It is a value-domain equivalence a customer confirmed out of band.

This is the structural claim, and the surface report now quantifies the need for it:
**35 of Birdeye's 89 operations require a value Birdeye itself never returns.** No
single-spec generator can plan those, because the producer is not in the document being
analysed.

### 2. Provenance, which the specification has no vocabulary for

`grep -ciE "provenance|confidence|uncertain|unknown"` over the whole of Arazzo 1.1.0
returns **0**. Across 1,727 lines there is no way to say where a step's parameter came
from or how much to trust it.

That is not a defect in Arazzo. It is describing a workflow someone already knows; a
fact you authored needs no provenance. It becomes load-bearing the moment the workflow is
**derived** rather than written, because then "the spec said so", "we reconstructed it
from source", and "a human vouched for it" are three different levels of trust that look
identical on the page.

We carry it as extensions — `x-gecko-provenance` on the Parameter Object, per edge:
`extracted` · `recovered` · `flagged` · `cross_surface`, plus the customer-confirmation
gate on a cross-API join.

### 3. Refusal — and the standard enforces it for us

Arazzo's Step Object **requires** exactly one of `operationId` / `operationPath` /
`workflowId`. Every member of `steps[]` is, by construction, a call. There is no
representation for a step that should not run.

So a plan with a hop we refused cannot be encoded as a degraded workflow. We emit **no
workflow at all**, which makes the document deliberately violate `workflows: minItems 1`.

The consequence, verified above: **a third-party linter we do not control rejects it.**
We do not need anyone to trust our refusal — the standard itself will not run what we
would not derive. Fail-closed, through someone else's runtime.

`x-gecko-refusals` carries the reason, where a human can read it and a runner cannot
execute it.

## What Arazzo cannot express, that we still need

Honest limits on the composition, not complaints:

- **Non-HTTP surfaces.** A Solana derive plan is dependency-ordered account derivation.
  There is no `operationId`, and the accounts are not calls. Arazzo has no shape for it,
  so our program-surface output is not exported as Arazzo at all — and should not be
  pretended into it.
- **Verification.** Arazzo describes a sequence; nothing in it simulates one. The receipt
  lives outside the document and always will.
- **The floor as a first-class idea.** We express refusal by absence plus an extension.
  It works and it fails closed, but "here is what I could not establish" is currently a
  Gecko concept riding on an Arazzo document, not an Arazzo concept.

## Where 1.1.0 goes next

1.1.0 adds a **Selector Object** (`context` + `selector` + `type`: JSONPath / XPath /
JSON Pointer). Today we encode a join as a runtime-expression string; a typed selector
with a declared language is a better home for exactly the thing we care about, and would
make the extraction checkable rather than conventional.

**We hold at 1.0.1 until a 1.1.0 JSON Schema publishes.** Shipping ahead of the schema
means we could not validate, and validating against the real schema is the entire
discipline.

## The one-line version

**Arazzo is the format. The hard part is deriving what goes in it, and saying honestly
where each part came from — including the parts you could not establish at all.**

Or, from their own hero image: **they drew the two-API workflow. We are the thing that
finds one.**

We should keep emitting Arazzo, keep validating against the real schema, and keep the
provenance and refusal extensions clearly marked as ours rather than implying the
standard blesses them.
