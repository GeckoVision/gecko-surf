# Step 0 — Design for agents (the API best-practices checklist)

**Status: provider-side guidance.** This step is upstream of Gecko. It is a checklist
the provider applies to *their own* API — not a Gecko feature. (The Gecko capabilities
start at [comprehend.md](comprehend.md).) Everything here is additive and standards-
adjacent: nothing breaks an existing consumer.

Steps 1–5 make a *finished* API agent-usable. This step makes the API **worth
comprehending** in the first place — so that when Gecko turns the surface into tools,
an agent lands the call on the first try because the surface itself is unambiguous.

The checklist below is distilled from a real design-partner review of a clean,
well-built API and **generalized** — it is not a "you're doing it wrong" list. On a
clean API most items are one field, one enum, or one doc string. Do the ones that
apply; skip the rest.

---

## A. Spec hygiene — the baseline that makes tools and codegen work

An agent (and Gecko) reads your OpenAPI mechanically. These make the machine read clean:

- [ ] **Unique `operationId` per operation.** Duplicates break SDK codegen and collapse
  two tools into one ambiguous name.
- [ ] **A `summary` *and* an intent-shaped `description` on every op.** The `summary`
  becomes the agent's tool name; a missing one yields an opaque tool. Make each
  description **distinct** — no two ops that are zero-overlap paraphrases of each
  other. Distinct, intent-shaped text is what keeps *lexical* retrieval honest (an
  agent shouldn't need vector search to tell two of your endpoints apart).
- [ ] **Enums for state/status fields.** Promote free-string status fields to an `enum`
  (e.g. `PEGGED|DRIFT|DEPEG|CRITICAL`). Agents can only branch safely on a closed set;
  include the sentinel values the live API actually returns (e.g. `UNKNOWN`).
- [ ] **Mark required params `required`; document defaults and units.** Anything the
  call fails without must be `required` in the schema, or the agent omits it. State
  units in the description when they're implied by prose (bps, USD, seconds).
- [ ] **One uniform error envelope, and say which field to branch on.** Return the same
  error shape everywhere and document that agents branch on the **stable machine field**
  (e.g. `error`), never the human-readable `message`.

## B. Design for the agent's decision — endpoints agents consume well

The single highest-leverage area. Agents don't browse; they hold an identifier and a
goal and need to land one call.

- [ ] **Designate ONE canonical read per resource.** If three endpoints return
  overlapping views of the same thing and each is missing *different* fields, an agent
  can't tell which to call and will guess wrong or over-fetch. Pick one as **the
  agent read**, make it complete (below), and in each sibling's `description` say so
  explicitly: *"For programmatic checks prefer `/canonical/path`; this endpoint is the
  web/detail view."* This costs a few doc strings and no deprecation, and it is usually
  the biggest single win on an otherwise clean surface.
- [ ] **Make the canonical read field-complete.** The read an agent leans on hardest
  should carry every field a caller needs to *act* — so it never has to pull a whole
  list and filter just to recover one missing field. A single-entity read that forces a
  whole-collection fallback isn't first-call-sufficient.
- [ ] **Disambiguate identity in the description.** If a resource is reachable by two
  identifiers (e.g. by-mint vs by-symbol, by-id vs by-slug), say **which identifier the
  caller holds at decision time**. An autonomous caller usually holds the machine
  identifier (an address, an id) — route it there and name the convention.
- [ ] **Expose freshness, and support conditional GET.** Put `updated_at` + a `stale`
  flag on time-sensitive reads so an agent can reason about staleness. Emit an `ETag`
  and honor `If-None-Match` → `304 Not Modified` on the single-entity reads a loop polls
  — cheap freshness for the poll-every-cycle consumer, and it protects your rate limit.
- [ ] **Fail loud, not silent.** A wrong filter value should `400` with a typed error,
  **not** return an empty `200`. An empty success is a silent wrong answer an agent
  reads as "no results." Normalize casing on filter params the same way you do on path
  params (`?class=LST` and `?class=lst` should behave alike).
- [ ] **Expose derived decision hints — additively.** If every consumer re-derives the
  same action from your data (e.g. `state ∈ {DRIFT,DEPEG,...} OR stale ⇒ risk-off`),
  expose that as one **citeable** boolean/enum you compute. Keep it a convenience *view*
  over the source of truth — **never** replace the underlying field with a lossy summary.
- [ ] **Cover the entities the agent ICP actually acts on.** Breadth on the long-tail
  entities your paying consumers hold beats more rows of the easy, well-covered class.

## C. Access agents can actually complete — auth + payments

- [ ] **One auth scheme, clearly scoped, so the public/gated boundary is
  machine-readable.** Keep gated operations under one obvious scope (e.g. all `/me/*`
  behind one scheme). A clean boundary lets a comprehension layer **hide** gated ops
  from an unauthenticated agent and **fail closed** if one is forced — instead of a raw
  spec dump exposing and mis-firing them.
- [ ] **Give agents a machine-authable path for anything an agent must do.** Never gate
  an agent-critical action (subscriptions, webhooks, writes) behind a **human-only**
  handshake (a Telegram/OAuth login widget). An autonomous or serverless caller can't
  complete it, so it degrades to polling. Offer an API-key or x402-gated alias of the
  same action; reuse the existing dispatcher — only the auth seam changes.
- [ ] **Monetize at your own endpoint with the standard x402 challenge.** For any op you
  want to charge for, answer an unpaid request with `402 Payment Required` at **your own
  `payTo`**. Gecko points the agent-facing tool at your endpoint and surfaces the
  handshake — you keep 100%, Gecko takes no cut. See
  [`x402-payai-setup`](../x402-payai-setup/SKILL.md).

## D. Verifiable — for oracle / data providers

- [ ] **Make the reading independently checkable at read time.** If agents trade or act
  on your data, the reading they act on — not just a state *transition* — should carry
  provenance: a `methodology` pointer, a stable calibration hash, and ideally a
  content-addressed `receipt_url` over the frozen inputs. An agent that must *cite* the
  reading it acted on will pay for provenance on the steady-state read, not only on
  alerts. This is on-brand for a data/oracle provider and Solana-native.

---

## E. Discoverable — make the surface self-describing

The breadcrumbs that let an agent *find* the agent-ready surface. Detail and exact
shapes: [artifacts.md](artifacts.md) and [discoverable.md](discoverable.md).
**Auto-emission by `gecko` is Building** — hand-author these today.

- [ ] **Enriched `llms.txt`** at your origin: the OpenAPI URL, the MCP URL, **the
  canonical agent read**, the identity convention (which identifier a caller holds), and
  which paths are human-only. One or two extra lines turn "the agent infers your
  conventions" into "the agent reads your conventions."
- [ ] **Optional `x-gecko` per-op annotations** (a vendor extension — additive, changes
  no behavior): mark `audience: agent|human`, `canonical: true` on the read you anointed,
  and the `key`/identity a caller holds. Leave it out and comprehension still works; add
  it and the ambiguous ops rank correctly without guessing.

---

## After the checklist: hand it to Gecko

Once the surface is designed to the above, Gecko does the rest without integration code:

1. **Comprehend** the whole spec → first-call-correct tools ([comprehend.md](comprehend.md)) — **Live**.
2. **Serve** the full surface over MCP with a one-click add ([serve-mcp.md](serve-mcp.md)) — **Live**.
3. **Emit breadcrumbs** ([artifacts.md](artifacts.md)) — **Building** (hand-authored today).
4. **Stay discoverable** without a public catalog ([discoverable.md](discoverable.md)) — **Building**.
5. **Aggregate, not replace** — your own MCP is never touched ([aggregate-not-replace.md](aggregate-not-replace.md)) — invariant.

---

## How a provider uses this kit (end to end)

A concrete path a provider can follow on their own API. It assumes you have an
OpenAPI 3.x URL (if you only have human docs, `gecko from-docs <url>` recovers a draft
first — see [comprehend.md](comprehend.md)).

**1. Install the plugin from the Marketplace.** In Claude Code:

```
/plugin marketplace add GeckoVision/gecko-surf
/plugin install gecko-surf@geckovision
```

This installs the skills, the `/make-agent-ready` command, and the
`api-onboarding-engineer` agent into Claude Code. The comprehension **engine** is a
separate, explicit install you run yourself: `pip install "gecko-surf[serve]"` (or the
zero-install `uvx` form in [comprehend.md](comprehend.md)).

**2. Run the onboarding spine on your spec.**

```
/make-agent-ready https://api.yourdomain.com/openapi.json
```

You get back the engine's live counts (operations ingested / tools surfaced /
auth-gated hidden), the served MCP URL, the one-click add strings, and the
hand-authored breadcrumbs to drop at your origin — with your own MCP left untouched.

**3. Walk the best-practices checklist above (Sections A–D)** against your spec and
close the items that apply. On a clean API these are small: a missing `summary`, an
enum, one canonical read anointed in three doc strings, a `304` on a hot read, a
machine-authable subscription alias. Re-run `gecko test <spec>` after changes to prove
the surface still lands first-call-correct.

**4. Make it discoverable (Section E).** Drop the enriched `llms.txt` and (optionally)
`gecko.json` at your origin, and add the `x-gecko` annotations to your spec. These are
hand-authored today (auto-emission is **Building**) and served from *your* domain — no
Gecko catalog is involved.

**5. Wire x402 if you want agents to pay (optional).** For priced operations, answer
with the standard `402` challenge at your own `payTo` and continue with
[`/setup-x402 <api>`](../x402-payai-setup/SKILL.md). Money flows agent → you; **you keep
100%**, Gecko takes no cut and is never the rail. Live settlement is founder-gated and
defaults to `X402_MODE=stub` — prove the paid-call shape offline first.

Done — your *whole* API is agent-usable and discoverable, not just the endpoints you
had time to hand-wrap, with your existing MCP still running beside it.

Back to the spine: [SKILL.md](SKILL.md).
