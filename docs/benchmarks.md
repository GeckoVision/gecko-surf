# Benchmarks — measured, dated, reproducible

Every number on this page is a **measurement with a date and a repro command**,
not a claim. Most run **offline at $0** (recorded mode / deterministic scripts),
so you can falsify them on your own machine. Nothing here is a guarantee — APIs
drift, and a benchmark is a snapshot.

## Why first-call-correctness is the metric

An agent that calls an API wrong doesn't fail loudly — it retries, hallucinates
params, or silently gives up. Gorilla (Patil et al., 2023,
[arXiv:2305.15334](https://arxiv.org/abs/2305.15334)) established both the
metric (AST-match correctness, not "did something return") and the baseline
reality: **zero-shot LLMs get real API calls right only a small fraction of the
time**. Gecko's bet is that the lift lives in the *surface* — comprehend the
spec into question-shaped tools and the same model calls the API right.

## Single-call correctness (the comprehension engine)

Agent-in-the-loop eval: an LLM picks and emits the call through Gecko's
comprehended tools; scored by the same well-formedness + AST-style match
discipline. Harness: `gecko/fcc_eval.py` (reports `hallucinated` and
`retrieval_recall_at_k` alongside `fcc`, per the Gorilla/RAT methodology).

| API | Scale | Result | Measured |
|---|---|---|---|
| **Stripe** | 587 operations (the rich-API control) | **99.8%** eval pass (586/587-scale sweep) | 2026-07-13 |
| **Twilio** | full public spec | **100%** eval pass | 2026-07-13 |
| **Jupiter Swap** | 4 ops, keyless live tier | first-call-correct incl. live smoke | 2026-07-10 |
| **TxLINE (TxODDS)** | 18 ops, paywalled, two-token auth | **32/32** `gecko test` checks (recorded, $0) | 2026-07-19 |

Reproduce (no keys needed for the recorded lanes):

```bash
gecko test <openapi-url>                 # first-call-correctness suite, $0
uv run python scripts/fcc_eval.py --help # the agent-in-the-loop eval harness
```

## Chain correctness (the surface graph)

A *plan* is only honest if the whole chain executes correctly — step N's output
must actually satisfy step N+1's input. The chain-FCC harness
(`gecko/chain_eval.py`) executes plans in recorded mode and scores every step
well-formed **and** every threaded value type-correct.

| Chain (TxLINE, real paywalled API) | Result | Measured |
|---|---|---|
| `fixtures/snapshot → odds/updates/{fixtureId}` via `FixtureId` | **first-plan-correct** | 2026-07-19 |
| `scores → stat-validation` via `seq` | **first-plan-correct** | 2026-07-19 |
| negative control: type-mismatched chain | **correctly fails** (string→integer id caught) | 2026-07-19 |

The negative control matters: a metric that can't fail isn't a metric.

```bash
uv run pytest tests/test_chain_eval.py tests/test_planner_wiring.py -q
```

Honest boundary: recorded synthesis proves **shape** correctness, not **value**
correctness — a live smoke is the final check, never the primary claim.

## The inference gate (did the graph earn the build?)

The `feeds` inference basis had to pass a falsifiable gate *before* the graph
was built: find every known chain on a real painful API **and** stay quiet on a
rich-API control. Three bases were tried; two were rejected on the data.

| Basis | Known TxLINE chains found | False-link edges on Stripe (587 ops) |
|---|---|---|
| v1 — name+type match | 1 of 2 | 66,984 |
| v2 — id-shaped only | 1 of 2 | 64,699 |
| **v3 — entity + genericity + id-shape** | **2 of 2** | **337** (−99.5%) |

Deterministic, offline, $0 — run it yourself:

```bash
uv run python scripts/surface_graph_probe.py
```

Every `feeds` edge ships with provenance (`EXTRACTED` vs `INFERRED`, basis,
confidence), so the residual 337 are *auditable*, not hidden — and a plan only
forms when an agent's intent actually needs the chain.

## What we don't claim

- No universal accuracy number — correctness is per-API and drifts with specs.
- No live-value guarantees from recorded mode (shape, not value).
- No cross-API chains yet — that inference must pass its own, stricter gate
  first (design: `docs/specs/2026-07-19-surface-graph-correlations-design.md`,
  §13). When it does, its numbers land on this page the same way: dated,
  scoped, reproducible.
