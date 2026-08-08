# Orchestration: Arazzo + SPDG — evaluation & phased plan

**Status:** plan (2026-07-29), from a staff-engineer architecture eval grounded in the code.
**Update 2026-08-07 — §1 EXPORT is SHIPPED** as `gecko/arazzo.py` (`to_arazzo`,
`is_executable`) + `gecko export-arazzo` + `McpSurface.export_arazzo`. See
[What shipping it settled](#what-shipping-it-settled-2026-08-07) at the foot of this
doc. Everything else below is unchanged and still open.

Answers: how does Gecko "close the e2e" on logic-between-APIs (Arazzo) and cross-API data
mapping (SPDG) while staying control-plane? Pairs with `docs/trust-boundary.md`,
`docs/specs/2026-07-19-surface-graph-correlations-design.md`.

## Bottom line

**Adopt Arazzo as an interchange *format* (export now, ingest later). Adopt the SPDG
*formalism* (we already ship ~80% of it). Reject the OpenSPG *engine* and Apache NiFi outright
— they are data-plane and break invariant #1. Gecko stays the deterministic planner + safety
layer that *emits* the plan; the agent runtime executes.** None of this is a new engine — it's
serialization of `cross_plan`/`compose_safe_chain` output plus a vocabulary enrichment.

## 1. Arazzo — export now, ingest later, never the executor

`cross_plan` already returns `Plan(steps, explain)` where each `PlanStep` carries
`operation_id/method/path/consumes/supplies/surface` and each `ExplainEntry` carries
`param/source_op/source_field/provenance/basis/confidence/source_surface`. **That is exactly an
Arazzo workflow** (`steps[]` with `operationId`, `parameters` from `consumes`, `outputs` from
`source_field`, `dependsOn` from `supplies`+`source_surface`, `sourceDescriptions` = the specs).
We already derive the thing Arazzo standardizes.

- **EXPORT — implement now (V2).** New pure module `gecko/arazzo.py`:
  `to_arazzo(Plan | SafeChainResult) -> dict` (serializer; no I/O, no payloads). Thin
  `gecko export-arazzo` CLI verb / MCP method. Buys real interop (Arazzo UI visualization,
  arazzo-cli execution, portability into any runtime) **without Gecko becoming a runtime.**
  The moat stays ours: anyone can hand-write Arazzo; only Gecko *derives* it from an untrusted
  spec with per-edge provenance, the DECLARED+CONFIRMED gate, and Skill-Guard quarantine.
  **One-way call (get it right first): a quarantined/refused hop must emit as a refusal marker,
  NEVER a callable step** — else the export inherits arazzo-cli's execution model and silently
  drops our safety gate. `SafeChainResult.summary`'s refusal must survive serialization.
- **INGEST — improve (V2.x).** Extend `hints.py` to parse a provider's Arazzo doc into
  name→entity DECLARED hints (structurally identical to `x-gecko`/`x-stripeOperations`).
  Untrusted → enters as a candidate, quarantined until customer-confirmed (rides the existing
  guardrail confirm loop); an ingested `dependsOn` must NOT mint a plan-eligible join. Deferred
  vs export because export is pure additive value with zero trust surface; ingest adds an
  untrusted parser and the payoff is speculative until a provider ships Arazzo in the wild.

## 2. SPDG / OpenSPG — adopt the formalism, reject the engine

`graph.py` already has typed nodes (`operation/param/field/resource`), typed edges
(`consumes/produces/on/feeds`), per-edge `provenance/basis/confidence`, a value-domain signature,
and `vindex.py` indexes by declared entity. **That is a lightweight SPDG — deterministic,
declared, control-plane.**

- **FORMALISM — adopt now, incrementally.** (1) More entity types beyond `solana-token-mint`
  (currencies, account/order ids, addresses…) — data + the `hints.py` confirm loop, not engine
  work. (2) Typed edge kinds — steal SPDG's `transforms`/`derives` distinction for the
  conversion case (op takes X, returns Y), as a `basis`/subkind on the existing `feeds` edge.
  Both describe the *surface*, never data → invariant-safe. Do them when a 2nd API demands it.
- **ENGINE — reject / defer to V3.** OpenSPG `kg-builder` extracts knowledge from DATA;
  `kg-solver` reasons over DATA; NiFi moves DATA. **All three are data-plane; adopting any
  wholesale violates invariant #1** (they join data *instances*, requiring stored response
  payloads — the exact thing that lets Gecko ingest any API unilaterally).
- **The control-plane line, stated precisely:** Gecko reasons over the schema-declared
  *surface* ("field `mint` on op A is entity `solana-token-mint`; param `mint` on op B consumes
  it"). Gecko must NEVER reason over response *values* ("the mint `So111…` returned by A equals
  the one B expects"). The moment a value instance enters the graph, you are OpenSPG's
  data-plane and you've traded away unilateral ingest. `Correland` (no payload field) enforces
  this structurally — keep that prohibition; it makes "formalism not engine" enforceable.

## 3. Orchestration model — planner emits, runtime executes

```
  Gecko (control plane)                 Agent runtime (data plane)
  ─────────────────────                 ──────────────────────────
  ingest → surface graph                arazzo-cli / LangGraph / AutoGen
  correlate → DECLARED joins                   │ executes the plan:
  cross_plan → derived chain                   │  - calls API A directly
  compose_safe_chain → quarantine gate         │  - carries A's output value
        └──── emits Arazzo 1.0 doc ───────────▶│  - calls API B directly
              (+ auth injected at call-time)   │  - Gecko never sees the values
```

**Gecko feeds LangGraph; it does not become LangGraph.** The "Planner Agent Layer" is the
runtime's job. Optional stateless harness (V2.5, flag loudly): running the safe-chain hop-by-hop
is *not automatically* a data-plane violation (invariant #1 is about STORAGE; `caller.py` already
builds real requests) — but carrying A's response value into B's call requires reading A's
payload in-process. That doesn't store, but it moves Gecko onto the data path. So: default =
planner emits, runtime executes; any harness must be pass-through only (extract the declared join
key, never persist, redact the rest) and an explicit opt-in exception, sold as such.

## Phased plan

- **(a) Implement now (V2):** `gecko/arazzo.py` export (quarantined hop → refusal marker, never a
  step); entity-vocabulary enrichment driven by API #2 (not speculatively).
- **(b) Improve — gaps this exposes:** request-body capture (the §13.5 mutate-chain gap — Arazzo
  export forces step `requestBody` inputs); **canonical param examples for verify-docs-live**
  (the Tier-1 finding — placeholder args falsely REFUTE real ops; Arazzo `successCriteria` needs
  real examples too); Arazzo ingest into `hints.py`; typed `transforms`/`derives` edges.
- **(c) Future / V3 (deferred, with reason):** the OpenSPG reasoning engine — data-plane,
  reconsider only over data the *agent* holds (never persisted in Gecko), under the trust tier.

**Modules:** new `gecko/arazzo.py` (serializer over `compose.Plan` + `safechain.SafeChainResult`);
`gecko/hints.py` (Arazzo ingest, later); `gecko/vindex.py`+`gecko/graph.py` (vocab + typed edges);
thin CLI/MCP verb. `Correland`'s no-payload shape is the guardrail to preserve.

**Open questions (founder):** (1) has any provider shipped a real Arazzo doc yet? If not, ingest
stays deferred and export is the only near-term move. (2) Is API #2 concrete enough to drive the
vocab enrichment, or defer that too?

---

## What shipping it settled (2026-08-07)

Building §1 EXPORT resolved four things the plan left open or got slightly wrong. The
plan's *decisions* all held; these are corrections of detail, recorded so the next reader
does not re-derive them.

**1. "A refusal marker" was underspecified — Arazzo has no such shape.** The plan said a
quarantined hop "must emit as a refusal marker, NEVER a callable step" without saying what
a marker *is*. Reading the normative schema settles it: Step Object is
`required: [stepId]` **plus** `oneOf: [operationId | operationPath | workflowId]`. Every
member of `steps[]` is by construction an invocation; there is no step that is not a call.
So the marker cannot live inside `steps` at all. What shipped: a refused chain emits **no
workflow** — the hops move to root `x-gecko-refusals` (+ `x-gecko-withheld`, which names
the clean hops too) and `workflows` stays `[]`, deliberately failing the schema's
`workflows: minItems 1`. An all-refused export is therefore **not a loadable Arazzo
document**, which is the strongest available statement of "there is nothing to run here"
and the only encoding a runtime cannot misread.

**2. The plan's field mapping was one field optimistic.** It read `dependsOn` as the home
of the join ("`dependsOn` from `supplies`+`source_surface`"). `dependsOn` is a
**workflow**-level field listing prerequisite *workflowIds* — it cannot express a
step→step data dependency. The real carrier is the pair
(`step.outputs`, `parameter.value`) bound by the runtime expression
`$steps.<stepId>.outputs.<field>`, which is also what keeps the export value-free.

**3. `Parameter.value` is REQUIRED, and that is fine.** Arazzo requires a `value` on every
parameter, which reads at first like a data-plane requirement. It is not: `value` accepts a
**runtime expression**, so every leaf we emit is a name reference
(`$inputs.…` / `$steps.…`). The "no values" line survives contact with the format intact.

**4. `SafeChainResult` could not carry the export on its own.** A `ChainNode` is a *hop*
(surface, op, tool, quarantine stance) — it has no `consumes`/`supplies` and no `explain`,
so serializing one produces steps with no parameters and no provenance. `SafeChainResult`
gained an additive `plan: Plan | None = None` (set by `compose_safe_chain`, control-plane
only) so the safety verdict and the edges travel together.

**Still open, unchanged:** Arazzo INGEST (§1, untrusted parser, deferred until a provider
ships one in the wild); the SPDG vocabulary enrichment (§2, wants API #2); the OpenSPG
engine (§3, still data-plane, still rejected).

**Newly exposed by building it** — each is a real gap the export *makes visible* rather
than one it creates:

- `PlanStep.consumes` is required non-auth inputs only, so an exported step names no
  optional parameter. Fine for the join; incomplete as a call template.
- `PlanStep` carries no parameter *location*, so `to_arazzo` needs the `SurfaceGraph`s to
  fill Arazzo's required `in`. Without them it refuses the workflow
  (`unresolved-parameter-location`) rather than guessing `query`.
- Step `outputs` point at `$response.body#/<field>`; ingest flattens nested response
  properties, so a nested join key resolves to nothing and the step fails loudly at the
  runtime. Recovering the real JSON pointer is the fix.
- `successCriteria` is omitted on purpose. The plan's §(b) already flagged that it needs
  canonical examples we do not have; inventing `$statusCode == 200` would be a guess.
- `requestBody.contentType` is emitted as `application/json` because ingest prefers that
  media type but does not record which one it actually took.
