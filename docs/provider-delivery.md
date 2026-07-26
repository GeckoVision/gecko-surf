# The first provider delivery — the Agent-Readiness Scorecard + Playground

**Status:** canonical product direction (2026-07-26), from a staff/CPO/CEO panel synthesis.
Pairs with `docs/positioning.md` and `docs/icp.md`. The GTM (pricing, sequencing) lives in
`private/` — this doc is the *product*: what we deliver to a provider and why it lands.

## The one artifact that answers everything

A provider's three questions — *what's the a-ha, what surface, what value* — collapse into a
single deliverable:

> **`gecko report <openapi.json>` → a self-contained Agent-Readiness Scorecard (HTML),
> with a chat Playground.**

Same class of artifact as the SVG call graph and the Skill-Guard outputs we already generate:
one file, no server, no hosting bill, runs in CI. **We are not hosting anyone's API** — we
ship them a scorecard and a *showcase*. No marketplace, no public catalog (a hard non-goal).

## What it shows (built vs. needs-work — honest)

| Section | What it says to the provider | State |
|---|---|---|
| **Agent-Readiness Score** | one number for how callable your surface is to an agent, + the specific gaps | **built** (`gecko inspect`: 4 weighted dims — first-call-correct 0.4, hygiene, agent-friendliness, security) |
| **The call graph** | your API as an agent actually traverses it — the *sequence*, with provenance on each edge | **built** (`gecko graph svg`) |
| **First-call-correct / failure spots** | exactly where an agent guesses wrong on your surface, each with a fix | **built** (dim 1 reuses `testgen`; findings carry location + fix) |
| **Ambiguity traps** | the params/endpoints an agent confuses (mint-vs-symbol, tip-floor) | **built** (dim 3) |
| **Anti-poisoning check** | is your surface safe for an agent to trust (Skill Guard) | **built** (dim 4 reuses `sanitize`) |
| **Correlation opportunities** | which of your outputs feed which inputs (and, across APIs, the chains) | **built, DECLARED-gated** — shines *across* surfaces; for a single spec, surface it as an upsell, not a core pillar |
| **The Playground** | *how an agent interacts with your API* — plain-English intent → the derived first-call-correct call → result | **new** (see below) |

**The most surprising frame** (the a-ha): the graph edge *they never documented* being
inferred correctly — *"it understood my API better than our own docs describe it."*

## The chat Playground — the interactive a-ha (context7-style)

The founder's addition: don't just score the API — **show an agent using it**. The Playground
is the interactive form of the scorecard: a chat box where a plain-English intent resolves,
through the comprehended surface, to the **right endpoint, the right params, first call
correct** — the same way an agent gets "used context7-style" by pointing at it.

- **V1 (self-contained, honest):** a *scripted* playground embedded in the scorecard — a
  handful of real intents → the derived calls (from `search_capabilities` / `plan_for` /
  `correlate`), replayed. No live LLM, no hosted API — it demonstrates the comprehension
  deterministically. Ships with the artifact.
- **V2 (interactive):** a live chat that runs the agent against the surface in `recorded`
  mode ($0, synthesized-from-schema) — real intent → real derived call → recorded result. A
  hosted flip, gated on provider WTP; never hosts the provider's live API.

## Re-evaluate on every update (drift-watch)

`surface_rev` already content-addresses a spec, so *"they changed their openapi.json"* is a
diff we detect. On change → re-run the deterministic `inspect` → **diff two reports = score
delta + added/resolved findings** ("your v4 broke 3 agent call-paths"). Built: `surface_rev`,
`inspect`, `graph`. Net-new: a report-diff + a `--since <rev>` flag (trivial) and, later, a
GitHub-Action trigger (the PR-comment bot — V1.1). The drift-watch is the *recurring* value.

## Build order

1. **`gecko report`** — HTML renderer over the existing `InspectionReport` + embedded `graph
   svg` + the scripted Playground section. Demoable this week.
2. **`--since <rev>`** — the report-diff (drift delta).
3. **PR-comment bot (V1.1)** — wraps the same artifact + the existing `--min-grade` exit code.
4. **Live Playground (V2)** — recorded-mode chat, WTP-gated.

## Do NOT build (the traps)

- A hosted multi-tenant App before a provider signals WTP (marketplace/infra trap).
- Storing provider **response payloads** to "prove" correctness — the scorecard is spec-only
  by construction (control-plane invariant #1).
- A **public catalog** of scored APIs — the report is *the provider's*, delivered to them.
- Over-indexing the single-spec scorecard on cross-API correlation — it's DECLARED-gated and
  shines across surfaces; upsell, not V1 pillar.

## The two honest gaps

1. The recurring drift-watch value is **aspirational until we show one drift catch
   end-to-end** — an early move is to *demonstrate* a spec change breaking an agent path and
   Gecko catching it.
2. Correlation needs a *second* API to be interesting — the single-provider scorecard leads
   with the score + graph + first-call-correct, and *teases* correlation.
