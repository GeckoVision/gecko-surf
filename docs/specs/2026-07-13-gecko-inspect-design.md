# `gecko inspect` — Agent-Readiness Inspector (Design Spec)

**Date:** 2026-07-13 · **Status:** design, for review (brainstorm → spec) · **Lane:** ai-ml + software-engineer

## Goal

`gecko inspect <api>` — a **provider-facing, offline, $0 agent-readiness scorecard**. Point it
at any API (a domain, an OpenAPI URL, a docs page, or a local path — via `resolve_spec`'s
auto-discovery) and get a **graded report** of how ready that API is for an agent to call
correctly the first time, with **concrete, located, fixable findings**.

It is **TDD-for-APIs**: run it before you ship, wire it into CI, gate the deploy on the grade
(exit non-zero below a threshold). The provider wedge — free — that opens the paid depth door.

## Non-goals

- **Not live testing.** It inspects the *surface*, never sends real upstream calls. Control-plane
  only — no payloads, no keys.
- **Not `gecko test`.** `gecko test` generates a consumer-side pytest of first-call-correctness;
  `inspect` *uses* that check as one of four dimensions and adds a provider-facing scorecard.
- **Not hosting / listing.** The "get listed, we host it" path is a separate product.

## The four dimensions (v1 = Full)

| Dimension | What it checks | Built from |
|---|---|---|
| **1. First-call-correct** | For each op, can an agent build a well-formed first call? (valid request + required-field guard) | reuse `testgen` |
| **2. Spec hygiene** | Unique `operationId`s; summaries present; params typed with `in`; required fields declared; auth described (`securitySchemes` + `security`); error responses documented + a consistent error envelope | **new** deterministic linter |
| **3. Agent-friendliness / ambiguity** | Near-duplicate **routing traps** (does the right op rank #1 for its own intent?); vague / non-question-shaped naming; path/query/body ambiguity | **new**, comprehension-native (reuse `catalog`) |
| **4. Security / anti-poisoning** | Injection / tool-poisoning red flags in descriptions + params | reuse `risk.py` |

### The ambiguity check (the differentiated, comprehension-native part)

For each operation, form an intent from its `summary`/`operationId`, run `catalog.search(intent)`,
and **flag when the operation is not the top-1 hit** — a sibling outranks it, so an agent asking
for this op's job may misroute. This is *exactly* the Jito `getTipFloor` (ranked #3) and the
Pegana mint-vs-symbol traps we hit live. It is deterministic (catalog ranking is pure) and
**only Gecko can do it, because it comprehends** — that is the moat expressed as a check.

## Scoring

- Each dimension → a sub-score (0–100, e.g. pass-rate for FCC/hygiene).
- **Overall grade** — a weighted aggregate rendered as a letter (A–F) + the number.
- **Findings** carry `severity` (`blocking` | `warning` | `info`), a `location` (op / param), a
  human message, and a concrete `fix`.
- **CI gate:** exit non-zero if grade `< --min-grade` **or** any `blocking` finding. Default
  `--min-grade` is a warning-level threshold (tune, see Open Questions).
- **Honest scoring** (project rule): the report states the real number and the real gaps — never
  a vanity green check.

## Output

- **Terminal** — a scorecard header (overall grade + per-dimension scores) then findings grouped
  by dimension, severity-sorted, each with its location + fix. Provider-facing voice ("Your API…").
- **`-o report.json`** — the full `InspectionReport` for CI + programmatic use.
- **Exit code** — for CI gating.

## Architecture (thin CLI, logic in `gecko/inspect.py`)

- **Input:** `onboard.resolve_spec` → inherits auto-discovery (domain) + `from-docs` (no spec) for
  free, plus SSRF validation.
- **Reuse:** `ingest` (parsed `Operation`s), `testgen` (FCC), `risk` (security), `catalog`
  (ambiguity), `tools` (the projected tool defs the agent actually sees).
- **New:** `gecko/inspect.py` — the hygiene linter, the ambiguity detector, the scorer, the report
  dataclasses, and the renderer; plus a thin `gecko inspect` subcommand in `cli.py`.

## Data contracts (dataclasses, single source of truth)

```python
Severity = Literal["blocking", "warning", "info"]

@dataclass(frozen=True)
class Finding:
    dimension: str            # "first-call-correct" | "hygiene" | "ambiguity" | "security"
    severity: Severity
    location: str             # operationId / "op.param" / "spec"
    message: str
    fix: str

@dataclass(frozen=True)
class DimensionResult:
    name: str
    score: int                # 0–100
    findings: list[Finding]

@dataclass(frozen=True)
class InspectionReport:
    api: str
    grade: str                # "A".."F"
    score: int                # 0–100 overall
    dimensions: list[DimensionResult]
    summary: str              # one-line headline
```

## Invariants

- **Offline, $0, deterministic** (same spec → same report — required for a CI gate).
- **Control-plane only** — inspects the surface; never sends a call, stores a payload, or a key.
- **SSRF** via `resolve_spec`.

## Wedge → depth (why it's on-thesis)

`inspect` is the **free provider wedge** — a provider *wants* to know if their API is agent-ready.
Its killer pairing is the **demand signal**: *"N agents are already failing on your API — here's
the report, and the fix."* The gaps it reveals sell the **paid depth** (drift-watch to keep them
fixed, anti-poisoning, hosting). So `inspect` and the attribution/instrumentation work reinforce
each other.

## Out of scope (v1)

- Live/behavioral checks (rate-limit behavior, real error bodies) — needs live calls, breaks $0.
- Auto-fixing the spec (inspect *reports*; `from-docs` *generates*) — a later "fix it for you" step.
- Historical drift ("what changed since last inspect") — that's the paid drift-watch product.

## Open questions (resolve during implementation / with real specs)

1. **Grade weights + thresholds** — tune against a spread: a clean spec (Stripe), a good one
   (Pegana), a rough one (a `from-docs` recovery), and a deliberately bad one. Blocking vs warning
   cutoffs should be calibrated, not guessed.
2. **Error-envelope consistency** — v1 checks that error responses *exist*; detecting a *consistent*
   envelope across ops (like Pegana's `{error,message,asset?}`) is a stretch check — include if cheap.
3. **`gecko test` overlap** — confirm `inspect` calls into `testgen` rather than duplicating FCC logic.
