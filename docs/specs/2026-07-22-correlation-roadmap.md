# Correlation & Multi-Call Roadmap — what's missing

**Date:** 2026-07-22
**Source:** `private/research/api-correlation-deep-research.md` (the deep-research
synthesis of the arXiv preprint outline + reading list), reconciled against what is
**actually shipped** in `gecko/graph.py`, `gecko/compose.py`, `gecko/planner.py`, and
`gecko/chain_eval.py`.

> This is a roadmap of gaps, not a re-statement of the thesis. The thesis
> (comprehension + correlations + anti-poisoning, combined) is in
> [`2026-07-19-surface-graph-correlations-design.md`](2026-07-19-surface-graph-correlations-design.md).
> Read this to know **what to build next and why**.

---

## 0. Reconciling the research doc with the codebase

The research doc's "Gaps and Future Work" (its §9) was written against the preprint
outline. Two of its claims are already **stale** — shipped since — and one describes a
model we do **not** implement. Getting this straight is the point of the roadmap: we
build the real gaps, not the paper's.

| Research-doc gap | Actual status | Evidence |
|---|---|---|
| #4 "Single-API only — can't correlate across APIs" | **SHIPPED** | `compose.cross_plan` (DECLARED-only cross-surface) + `chain_eval.evaluate_cross_chain`; proven Birdeye × daily-news |
| Provenance is a **3-tier** ladder `EXTRACTED > INFERRED > SUSPICIOUS` | **Only 2 derive-tiers exist** | `graph.Provenance = Literal["EXTRACTED","DECLARED","INFERRED"]`. There is **no `SUSPICIOUS` provenance value**; suspicion lives in the **sanitizer/quarantine** path (`sanitize.py`), not the graph. The doc conflates the two. |
| #1 recorded proves shape not value | open | recorded mode synthesizes from schema; no live correlation check |
| #2 supplier tiebreak is cost-then-lexicographic | open | `graph._PROV_RANK` + the §13.2 ladder; no semantic tiebreak |
| #3 body-carried join keys not planned | open | no `requestBody` correlation in `graph.py`/`planner.py` |
| #5 name inference is English/latin-biased | open | `catalog._tokens` + entity regexes are ASCII/English |

**Doc-hygiene follow-up (separate):** the marketing framing in the research doc's §4/§10
should be corrected to say "EXTRACTED / DECLARED / INFERRED, with a *separate* sanitizer
quarantine tier" rather than implying a single `SUSPICIOUS` provenance rank — otherwise a
reader (or a slide) claims a model the code doesn't have. Tracked here, fixed in the doc.

---

## 1. Shipped (the baseline this roadmap builds on)

So the roadmap starts from truth, not aspiration:

- **Entity-ID scoping** (`graph._entity_of`, planner `_identifying_tokens`) — `{id}` under
  `/users/{userId}/posts/{id}` scopes to Post, not User.
- **Genericity demotion by frequency** — a field in ≥ `max(4, ⌈0.03·n⌉)` operations is
  demoted; on the Stripe control this cut false links **66,984 → 337 (−99.5%)**.
- **ID-shape / value-domain signature** (§13.1 `type|fmt|pat8|enum8`) — `userId` (UUID) ≠
  `id` (int) even when both are named "id"; a signature only ever **corroborates**, never
  creates, a cross-surface edge.
- **Per-edge provenance** `EXTRACTED | DECLARED | INFERRED` + basis + confidence, ranked
  `_PROV_RANK`, with `DECLARED > INFERRED+signature > INFERRED-name`.
- **Intra-API chain planning** (`planner.plan_for_query`) — a plan block reaches the agent
  only when the top op's inputs aren't satisfiable from intent.
- **Cross-API planning** (`compose.cross_plan`) — two per-surface graphs joined **only** on
  a `DECLARED` entity the provider vouches for; a name collision cannot invent a link.
- **Anti-poisoning** — the sanitizer + per-tool quarantine + fail-closed auth routing are
  the security layer *under* the graph; a poisoned spec can at worst create a DECLARED-or-
  INFERRED edge, never an EXTRACTED one.

---

## 2. V2 — near-term (each item is one shippable slice with a falsifiable test)

Ordered by leverage. Every item ships behind a test that fails first, per Pattern B.

### V2.1 — Body-carried join keys  ·  **highest leverage**
**Gap:** correlations hidden in `requestBody` schemas, not URL/query params, are invisible
to the planner. This is the one that blocks *mutate* chains (create X → use X.id in the
body of create Y), which is most of the interesting cross-API work.
**Build:** extend `graph.required_inputs` / `opnode` to walk `requestBody` object schemas
and mint `feeds` edges into body fields, same provenance rules as path/query.
**Falsify:** a two-op fixture where op B's join key lives only in its request body; assert
the plan recovers the chain and that a body field with a mismatched signature does **not**
link. Guard: this widens the poisoning surface — route through `defi-security-engineer`
before merge (a body `default`/`example` is attacker-controllable just like a param one).
**Done when:** a create→create chain plans first-correct in recorded mode.

### V2.2 — Semantic supplier tiebreak
**Gap:** when several ops can supply a field, the tiebreak is cost-then-lexicographic —
arbitrary. Picking the wrong supplier is a first-call-*wrong* even when the graph is right.
**Build:** an **LLM-free** tiebreak using field-description token overlap + path specificity
(a `GET /users/{id}` beats a `GET /search?q=` for supplying `userId`). Keep it deterministic
— no model in the hot path (invariant: the engine stays offline-falsifiable).
**Falsify:** a fixture with two valid suppliers of one field where the lexicographic pick is
wrong; assert the semantic pick is chosen and the plan is first-correct.

### V2.3 — Live correlation validation (opt-in)
**Gap:** recorded mode proves **shape**, never **value** — we can't confirm a correlation
actually resolves without running it.
**Build:** a rate-limited, opt-in live probe that runs a planned chain against the real API
and records outcome **metadata only** (did field F from op A satisfy param P of op B) — no
payloads (invariant #1). This is the first real feed for the V2 correctness corpus, so it
belongs behind the `data-engineer` feedback-path decision, not bolted on.
**Falsify:** offline first — a fake transport that returns a known chain; assert the
validator records "resolved" without storing any response body.

### V2.4 — TaskBench precision/recall harness  ·  **publishable**
**Gap:** we report Stripe −99.5% and TxLINE first-plan-correct, but we don't score inferred
`feeds` edges against a ground-truth tool graph.
**Build:** score `graph`'s inferred edges against TaskBench's human-verified tool graph
(precision/recall of inferred edges). This is the metric the preprint needs and the number
that answers "how good is the inference, really".
**Falsify:** the harness itself is the deliverable; wire it into CI as a tracked benchmark,
not a one-off.

---

## 3. V3 — medium-term (the "beyond a static DAG" tier)

Deferred deliberately; each needs V2 shipped first.

- **Temporal dependencies** — infer "call A must complete before call B" from the spec
  (status transitions, `202 → poll` patterns), not just data-flow edges.
- **Rate-limit correlation** — infer which ops share a rate-limit bucket, so a plan can
  order calls to avoid a 429 mid-chain.
- **Error-recovery graphs** — infer fallback edges ("if A 404s, try B"), so a chain can
  self-heal instead of dead-ending.

## 4. V4+ — long-term (research)

- **Multilingual entity recognition** — de-bias the name-based inference off English/latin;
  the value-domain signature (§13.1) already gives a language-agnostic corroborator to lean
  on, which is the wedge.
- **Cross-organization correlation** — correlate APIs across *different* providers, still
  DECLARED-gated so it never becomes an uncurated public join.
- **Adversarial robustness proof** — formalize that the correlation inference is resistant
  to spec poisoning (the provenance ladder as a stated, tested guarantee, not a claim).

---

## 5. What this roadmap is NOT

Guardrails, so scope doesn't drift into the two things the thesis forbids:

- **Not a payment rail, not a marketplace.** Cross-org correlation stays DECLARED-gated and
  curated; we never publish an uncurated public catalog of joins.
- **Not an LLM in the hot path.** Every V2 inference item is deterministic and offline-
  falsifiable. The moment a plan needs a model at runtime, stop and re-read the invariant.
- **Not a corpus moat claim.** Live validation (V2.3) seeds the corpus, but per the
  three-pillar decision the corpus is an *execution* advantage, not a data moat — don't
  market it as the latter until the flywheel is proven on one API.

---

## 6. Immediate next action

**V2.1 (body-carried join keys).** It's the highest-leverage gap, it unblocks mutate
chains (the interesting half of cross-API), and it has a clean falsifiable test. Route the
poisoning-surface review through `defi-security-engineer` before merge.
