# Semantic depth — the comprehension-derived signal (brief 4b)

Date: 2026-07-09
Owner lane: `ai-ml-engineer` (spec→first-call-correct + the correctness eval)
Status: SPEC / DESIGN. No implementation in this document. Engine files untouched.
Extends: PRD-roadmap-coordination PART 1 §4 + PART 4 §4b; pairs with
`2026-07-09-context-hub-adoption-retrieval.md` (retrieval + BM25-remap) and the
`context-engineer` spec (4a, the context contract — the OTHER half of the centerpiece).
Ties: memory `context-engineering-anthropic`, `security-gateway-thesis`.

This is centerpiece half 2. Half 1 (context-engineer) owns what the agent **reads**;
this owns what Gecko **derives** from the parsed surface — the uncopyable input that
feeds BOTH product payoffs: correctness (right call, first try) and governance (the
gate blocks the harmful call). One input, two payoffs; you cannot bolt governance onto
a proxy that does not comprehend the API.

---

## 0. TL;DR for the founder (~10 lines)

- **The question answered:** comprehension-derived semantics pays down to *layer 3*
  (HTTP method → path/operationId tokens → request-body arg-shape co-occurrence).
  Below that it is high-precision and offline-falsifiable **now**. At *layer 4*
  ("anomalous args learned from a value distribution") precision collapses at our
  18–26-op scale — there is no distribution to learn from — so it is **V2, gated on
  the corpus**, not shippable today.
- **Transfer/spend-tier classifier:** derived from the spec, not the HTTP verb — a
  weighted feature vote over path/operationId money-verbs + a required numeric
  amount-shaped field co-occurring with a recipient/destination-shaped field.
  **Precision target ≥ 0.95** (false transfer-tier can BLOCK a paying call), recall
  target ≥ 0.80. **Fail-closed = never block on tier alone**: tier is a *step-up* weight;
  a BLOCK needs tier=transfer **AND** an explicit policy predicate (over-cap /
  recipient-not-allowlisted) — which bounds the false-positive blast radius.
- **Each depth layer must earn its delta on a frozen golden set before it ships.** The
  tier layers are falsifiable offline at $0 today; the anomaly-from-distribution layer
  is not until `data-engineer` supplies observed failure classes.
- **The owed experiment is pre-registered here** (Gecko vs a bare agent on a spec-less
  painful API): FCC delta + tokens-to-first-correct-call, with a NULL result we accept
  and the honest possibility the bare agent **wins on well-documented specs** — fine,
  our ICP is spec-less/painful and the experiment must be able to fail.

Top 3 founder decisions in §7.

---

## 1. The central question (answered up front)

> *How deep can comprehension-derived semantics go before signal precision collapses —
> and what measured delta over a bare agent / lexical baseline does each layer of depth
> buy?*

Depth is a ladder. Each rung adds recall for a harder signal and costs precision. The
honest engineering answer is where the curve bends:

| Depth layer | What it derives | Input needed | Precision posture | Measured delta it must buy | Ship gate |
|---|---|---|---|---|---|
| **L0** HTTP-method tier | read < write < destructive | `method` | high, **low recall for transfer** (POST /payments ≡ POST /notes) | shipped baseline (`_op_risk`) | live |
| **L1** path/operationId money-verbs | transfer/spend *candidate* | `path`, `operation_id` | med (ambiguous tokens: "order", "charge") | tier-recall lift on the tier golden set, precision floor held | **now, gated on §1 targets** |
| **L2** request-body arg-shape co-occurrence | transfer/spend *confirmed* (amount ∧ recipient) | `request_body`/`parameters` schema | **high** (the sharpest transfer signal) | tier-precision ≥ 0.95 at recall ≥ 0.80 | **now, gated** |
| **L3** semantic anomaly — wrong-op-for-intent | intent/op mismatch | retrieval margin (already have) | high (OOS_pass already 1.00) | no new false-block on paraphrases | **now, reuses fusion floor** |
| **L4** anomalous-args-for-op from a value distribution | "this amount is 100× the norm" | a per-op observed distribution | **collapses at 18–26 ops** — n too small, hallucinates | corpus lift on `fcc_eval` | **V2, corpus-gated** |
| **L5** corpus-observed failure classes | "this exact call failed before, here's why" | `data-engineer` corpus rows | only *real* ground truth | `lift_corpus` > 0 on `fcc_eval` | **V2, flywheel-gated** |

The collapse is at **L4**: an "anomalous argument value for this op" signal needs a
learned distribution of prior values, and (a) we do not store values (invariant #1),
(b) at 18–26 ops with no traffic there is no distribution. Any L4 signal built today
would fabricate a threshold and false-positive paying calls. So L4/L5 are **corpus-gated
V2** — they turn on only when `data-engineer`'s observed-first rows exist and
`fcc_eval` shows `lift_corpus > 0`. Everything L0–L3 is spec-derivable and offline-
falsifiable at $0 **now**.

This table is the spine of the whole spec. Sections 1–3 build L1–L3; section 4 is the
protocol that measures the top-line delta over a bare agent.

---

## 2. Per-op risk-tier derivation from the parsed spec (L0→L2)

### 2.1 The tier ladder (single source of truth)

Three tiers, already implied by `enforce.WRITE_METHODS` and `risk._op_risk`; this
promotes tier from "HTTP verb" to "comprehended operation":

- **read** — no upstream state change (GET/HEAD, and OPTIONS).
- **write** — mutates upstream state, no value movement (POST /notes, PATCH /profile).
- **transfer / spend** — mutates state **and** moves value or issues an irreversible
  external effect (POST /payments, POST /withdrawals, POST /orders, PUT /transfers).

`transfer` is a strict subset of `write`. The gradient is why governance is must-have,
not nice-to-have (PRD §1): a confidently-wrong read wastes tokens; a transfer drains a
wallet. Tier is the single term the governance predicate (spend cap, recipient
allow-list) keys on, so it must be one derivation, imported everywhere — never
re-computed in the gate.

### 2.2 Why HTTP method is not enough

`POST /payments/{id}/capture` and `POST /notes` are the same verb. Method gives L0
recall ≈ the fraction of transfers that are the *only* POST on the API — near-useless on
a real payments/on-ramp API where everything is POST. Depth is exactly the recall that
method leaves on the floor.

### 2.3 The classifier — features (a weighted vote, not ML)

Deterministic, pure, offline, spec-only. No model, no network (invariant #2 — this must
reduce to `data(spec)`). It is a scored vote so a single ambiguous token cannot flip a
tier; the sharp signal (L2 arg-shape) dominates.

**Feature A — money-verb in path/operationId (L1).** Tokenize `path` + `operation_id`
with the catalog identifier tokenizer (camelCase/alnum split — the chub-shaped tokenizer
from the retrieval spec §1c; reuse it, do not re-invent). Match against a curated,
**narrow** money-verb lexicon (deliberately narrow for the same reason
`_INJECTION_MARKERS` is narrow — an over-broad verb false-positives and blocks a paying
call):

> transfer, transfers, send, withdraw, withdrawal, payout, payouts, disburse,
> remit, wire, settle, settlement, charge, capture, refund, redeem, spend, debit,
> checkout, purchase, `pay` (whole token only — never substring, or "payload"/"payment-
> status" trip it), swap, mint, burn.

Ambiguity guards baked into the lexicon design, not code review afterthought:
- `order` is EXCLUDED from the lexicon (sort-order vs purchase-order — too noisy). A
  purchase op is caught by L2 arg-shape instead.
- `pay`, `charge`, `capture` fire only as **whole tokens**, never substrings.
- A verb in a GET path (`GET /payments`, a *listing*) does NOT lift tier above read —
  Feature A is only consulted when the method is state-changing (`is_write_method`).

Feature A alone = a *candidate* (medium precision). It never confirms transfer by itself.

**Feature B — arg-shape co-occurrence in the request body/params (L2, the sharp one).**
Inspect `request_body` schema + `parameters`. Confirm transfer when BOTH shapes co-occur
on the same op:

- an **amount-shaped required field**: name ∈ {amount, value, quantity, qty, price,
  total, sum, cost, fee} (whole-token, camel/snake-split) **and** JSON type ∈
  {number, integer} **and** the field is `required`; OR a field whose name contains
  {amount, price} regardless of required.
- a **recipient/destination-shaped field**: name ∈ {recipient, to, destination, dest,
  payee, beneficiary, address, wallet, account, iban, counterparty, receiver} (with the
  same whole-token discipline; `to` only as an exact field name, never a substring).

The co-occurrence (a numeric amount **and** a recipient on a state-changing op) is the
signal a signature scanner and a DeFi-trade firewall structurally cannot compute — it
requires the comprehended body schema. This is the money shot and carries the precision.

**Feature C — security/scope hints (weak corroboration).** An op whose `security` names
a payment/write scope (`payments:write`, `transfers.create`) corroborates. Never
decisive alone (many APIs scope everything identically); a low-weight tie-breaker only.

### 2.4 Scoring, thresholds, and the tier decision

A per-op vote → tier, computed once at ingest-adjacent time and attached as comprehension
metadata (NOT stored as a payload — it is surface metadata, invariant #1 clean):

```
method not state-changing        -> read      (Features A/B not consulted)
state-changing:
  B (amount ∧ recipient)          -> transfer  (confirmed; the sharp path)
  A (money-verb) ∧ (amount|recip) -> transfer  (verb + one half-shape)
  A alone (money-verb, no shapes) -> write, tier_confidence=low, verb_flag=true
  none of the above               -> write
```

`tier_confidence ∈ {high, low}`. `high` = confirmed by B, or A+one-shape. `low` = a
lone money-verb, or a lone amount-field with no recipient (could be a metered write, not
a transfer).

### 2.5 How tier feeds `score_call` — a signal, NOT a new gate

Per the brief and invariant: **do not add a gate; feed the existing pure interface.**
Tier becomes one more `Reason` inside `_op_risk` (or a sibling signal function that
`score_call` already composes the same way as `schema`/`poison`/`exfil`/`op`). Concretely
`software-engineer` implements, behind `score_call`'s pure inputs, a signal that
replaces the flat `_op_risk(method)` weighting with a tier-aware one:

| Tier | Reason signal | Points (design intent) | Decision effect at default thresholds |
|---|---|---|---|
| read | (none) | 0 | allow |
| write | `op.write` | 15 (unchanged) | allow |
| write (destructive DELETE) | `op.destructive` | 30 (unchanged) | step_up-ish |
| transfer, `high` conf | `op.transfer` | 25 | **step_up, not block** |
| transfer, `low` conf | `op.transfer_maybe` | 12 | allow-ish |

**Critical design rule — tier never blocks on its own.** 25 points sits below the
default `block_at=60`, so a merely-transfer op is a **step_up** (a warning the agent
reads, PRD §3 refusal-payload path), never a hard block. A BLOCK on a transfer only
happens at the **intersection** with a governance predicate authored in the AgentPolicy
work (spec 4d / governance-identity design): `tier == transfer AND (amount_over_cap OR
recipient_not_allowlisted OR scope.not_allowed)`. That intersection is where
`scope.not_allowed` (45) or a spend-cap signal stacks tier over `block_at`. This keeps
the two payoffs cleanly separated: **tier is comprehension** (this spec), **the block
predicate is policy** (that spec), and tier alone is never load-bearing for a refusal.

`transfer` is NOT added to `BLOCKING_SIGNALS` (that frozenset stays exfil/injection/
quarantined — genuinely categorical harms). Tier is additive/threshold, deliberately.

### 2.6 The false-positive cost, stated plainly

A false `transfer` label on a benign paying call costs, at worst, a **step_up warning**
— annoying, not fatal — *unless* the operator has set a spend cap AND the amount extracts
as over-cap. That double-condition is the actual "blocks a paying call" risk. Therefore:

- **Precision target: ≥ 0.95** on the tier golden set (§2.7). Measured as: of ops labeled
  `transfer` by the classifier, ≥ 95% are true transfers. A false-transfer is the
  expensive error; we tune the lexicon/thresholds to minimize it, accepting recall loss.
- **Recall target: ≥ 0.80** — catch ≥ 80% of true transfers. A missed transfer degrades
  to `write` (still step_up on DELETE, still scored) — a safety miss the on-chain
  firewall (devnet backstop) is the last resort for. We accept lower recall to protect
  precision, because the business cost of a false block outranks the safety cost of a
  missed step-up in the OFF-CHAIN tier (the on-chain firewall owns the un-missable case).

### 2.7 Fail-closed default — the two directions, resolved

There are two opposite "fail-closed"s and conflating them is the trap:

- **Business fail-closed (don't block paying calls):** when tier is *ambiguous*, classify
  **DOWN** to `write` with `tier_confidence=low`. A low-confidence transfer never blocks.
- **Safety fail-closed (don't wave a dangerous write through):** already owned by
  `enforce.fail_closed_refusal` — a state-changing op that cannot be *scored at all*
  (scorer/policy crash) is refused. That is orthogonal to tier: it fires when scoring
  fails, not when tier is uncertain.

Resolution: **tier classification is high-precision (fails DOWN); the amount extraction
that would push a transfer over a spend cap fails SAFE (if we cannot parse an amount, we
cannot assert over-cap, so we step_up — warn — rather than block).** The un-missable
drain case is the operator's explicit recipient-allow-list / hard-cap predicate (spec 4d)
plus the on-chain firewall — narrow, opt-in, extremely-high-precision rules, not the
broad tier classifier. This is the seam that keeps "governance" from drifting into a
"generic agent firewall" (PRD risk): every tier signal traces to the parsed body schema.

### 2.8 Ground truth for the tier eval — a gap to close first

**Neither committed golden spec has a real transfer op.** txodds and pegana are
read-heavy (odds/peg-state reads; the only writes are auth/activate). So the tier golden
set needs a **transfer-bearing spec**. Proposed source, in preference order:

1. **Nora Finance** (memory `nora-finance-integration`) — BRL↔BRS on/off-ramp is a
   genuine transfer API; ingest the SPEC only (no key needed for offline tier labels).
2. A committed **Stripe-subset fixture** (charges/transfers/refunds/payouts) — synthetic
   but canonical money-verbs and amount∧recipient bodies.
3. A minimal hand-authored `payments_tier.json` OpenAPI with labeled read/write/transfer
   ops if 1–2 stall (unblocks the falsifier without external dependency).

Deliverable: a frozen `tests/fixtures/golden/tier_labels.jsonl` — `{operation_id, tier}`
per op across ≥ 2 specs spanning all three tiers, sha256-pinned like the existing golden
sets. This is the **first thing the Pattern-B falsifier needs** (§6).

---

## 3. Semantic anomaly signals feeding `score_call` (L3)

Two anomalies, both fed through the **existing** pure interface — no new gate,
`apply_gate` untouched. Both are already largely computable from signals we have.

### 3.1 "Wrong op for this intent" (L3 — reuse the retrieval margin)

Signal: the agent invoked op X, but for the stated goal the retrieval substrate ranks X
far below the top and with no lexical corroboration. This is the *inverse* of the
out-of-scope guard already anchored in `fusion.py`/`client` (the lexical-anchored
confidence floor). Design:

- The gate does not always have the goal string. When it does (the hosted call path can
  carry the originating intent), compute `search_scored(goal)` and check whether the
  invoked `tool_name` appears with a genuine (non-fallback, score>0) hit. If the invoked
  op is absent from the genuine hits **and** some other op is a strong genuine hit, emit
  `intent.mismatch` (low-to-moderate points — a step_up, never categorical; a paraphrase
  is not an attack).
- **Precision guard:** this fires ONLY when there is a genuine competing hit, never on a
  thin/ambiguous surface (the same discipline that keeps `OOS_pass_rate` at 1.00). No
  goal string → signal degrades to silence (per-signal crash-containment already covers
  "no input"). This must NOT regress `oos_pass_rate` — that is its ship gate.
- Below the surface-all gate (≤50 ops) this is weak (the agent saw every tool anyway),
  so its real value is above the gate — flag it as **measured-lift-gated**, same posture
  as BM25 in the retrieval spec.

### 3.2 "Anomalous args for this op" (L2 confirmed / L4 deferred)

Split by what precision the input supports:

- **L2 (ships now) — schema-anomalous:** already computed by `_schema_conformance`
  (`schema.required`, `schema.unknown_field`, `schema.type`, `schema.enum`). This is the
  strongest, cheapest anomaly signal and it is *already live*. Semantic depth here is not
  a new signal — it is confirming this is the load-bearing one and keeping the tier
  classifier from double-counting it.
- **L4 (DEFERRED, corpus-gated) — distribution-anomalous:** "this amount is 100× the
  typical value for this op," "this recipient host has never been seen." Requires a
  learned per-op value distribution we do not have and (invariant #1) do not store. At
  18–26 ops with no traffic this **collapses** — any threshold is fabricated and
  false-positives paying calls. **Do not build until `data-engineer`'s observed-first
  corpus exists** and `fcc_eval`'s `lift_corpus` shows it earns its keep. This is the
  precise "how deep before precision collapses" boundary from §1.

### 3.3 The interface contract (for `software-engineer`)

Every anomaly is a `Reason(signal, points, message)` returned by a pure function
`score_call` already composes via `_run_signal`. Semantics specified here; the code lives
in `risk.py` behind the existing pure-inputs contract. Constraints the implementer holds:
crash-containment per signal (already there), no signal added to `BLOCKING_SIGNALS`
without a categorical-harm justification, and points chosen so no *single* anomaly crosses
`block_at=60` alone (blocks come from stacking or an explicit categorical/policy hit).

---

## 4. Intent→call grounding quality (the golden-set eval + the >50-op gate)

This section defers to and does not restate `2026-07-09-context-hub-adoption-retrieval.md`.
Load-bearing facts carried forward:

- **Current substrate recall@3: txodds 0.67, pegana 0.60 — both < 0.8.** But both are
  18/26 usable ops (< 50), so `scale.should_surface_all` shows the agent *every* tool and
  **substrate rank is decoupled from FCC** below the gate. A better ranker buys a prettier
  recall number, not a better first call, at this scale. This is why we do not chase
  recall@3 now.
- **The hybrid gate is an AND:** `usable_ops > 50` **AND** `recall@3 < 0.8`. Only the
  recall half is met; op-count is the binding un-met condition. No hybrid today.
- **When the gate fires, the sequence is: step 1 swap overlap→BM25 with OpenAPI-remapped
  field weights** — chub's `id: 4.0` prior is **inverted** for us (our operationIds are
  auto-generated garbage like `getApiOddsSnapshotFixtureid`; summary is the intent-bearing
  field). Remapped weights: summary high, tags/description mid, **operationId low**. Step 2
  (dense+RRF, already built OFF in `dense.py`/`fusion.py`) only if BM25 alone leaves
  recall@3 < 0.8. chub running pure-BM25 at registry scale is evidence the dense arm may
  stay shelved even then.
- **recall@3 is the grounding metric** the eval publishes per surface; it is measured on
  `search_scored` (the pure substrate) via `evaluate_golden`, kept distinct from the
  agent-facing surface-all path so a disclosure change never confounds a recall reading.

Grounding-quality's contribution to THIS spec: it is the L3 substrate. The "wrong op for
intent" anomaly (§3.1) reuses exactly this ranked list and its lexical-corroboration
floor. Do not build a second ranker for the anomaly signal — it consumes the catalog's
`search_scored` output.

---

## 5. The pre-registered eval protocol for the OWED EXPERIMENT

The claim under test (PRD §7, the edge claim we keep OUT of outward copy until this
reports): **"Gecko makes an agent first-call-correct on a spec-less painful API where a
bare agent is not."**

This is pre-registered — protocol, metrics, and decision rule written down BEFORE the run
so the result cannot be rationalized after the fact. The experiment **must be able to
fail**, and one failure mode (bare agent wins on well-documented specs) is not a bug.

### 5.1 The two arms

- **BARE (control):** the same cheap model (Haiku, matching `fcc_eval`) given the API's
  **native human artifacts** — its docs URL / raw OpenAPI dump / whatever a developer
  would paste — and the goal. No Gecko. This is the honest baseline: what a coding agent
  one-shots today. (Note: this is a *harder* baseline than `fcc_eval`'s RAW arm, which
  dumps the parsed spec as tools; BARE models the realistic "point the agent at the docs"
  workflow.)
- **GECKO:** `client.search(goal)` → question-shaped, auth-hidden, retrieval-surfaced
  tool defs → the gate → prepare. The shipped path.

Both arms, same model, same goals, same `n_runs` (≥ 3 — Haiku is non-deterministic;
reuse `run_variance`). One tool-use turn each, scored by the shared caller guard +
`args_match` (reuse `fcc_eval.score` — do not fork the scorer).

### 5.2 The API selection — what "spec-less / painful" means (pre-committed criteria)

The experiment is only valid on our ICP. Pre-commit the selection so we cannot cherry-pick
a favorable API after seeing results. An eligible target meets **≥ 3 of**:

1. No committed OpenAPI, or an incomplete/hand-written one (docs are prose/HTML).
2. Auto-generated or absent operationIds; thin/empty summaries.
3. Non-obvious param routing (the mint-vs-symbol class — a value could go in ≥ 2 slots).
4. A paywall / auth handshake that gates the real call.
5. Drift (the live surface diverges from any published doc).

Candidates from memory: **Pegana** (warm design partner, already integrated + critiqued),
**Nora** (on/off-ramp, apiKey-gated, real transfers — doubles as tier ground truth). We
pre-register the target list **before** running; the well-documented control API (below)
is chosen to make the null *possible*.

### 5.3 The honest counter-arm — a well-documented API where the bare agent may win

Include ONE clean, well-documented API (e.g. a canonical Stripe-subset or a blue-chip
REST API with a pristine spec) as a **negative control**. Prediction, pre-registered:
**on the clean API, GECKO's FCC delta over BARE is ≈ 0 or negative** (the bare agent
already one-shots it; Gecko's surfacing adds nothing and its top-k could even hide an op).
If the delta is large-positive on the clean API too, that is a *surprising, investigate-
first* result — likely a baseline bug, not a Gecko win. This arm is what makes the
experiment falsifiable and keeps us honest about the ICP boundary.

### 5.4 Metrics (primary + secondary)

- **PRIMARY — FCC delta:** `fcc_rate(GECKO) − fcc_rate(BARE)` over positive tasks, using
  the existing `tool_correct ∧ well_formed ∧ args_match` definition. Reported per API with
  `run_variance` (mean ± stdev) so a delta inside noise is called noise.
- **SECONDARY — tokens-to-first-correct-call:** total prompt+completion tokens consumed
  until the FIRST call that scores `fcc=True` (summing retries if the arm retries). Gecko's
  thesis is not just "more correct" but "correct **sooner/cheaper**" — auth-hiding + top-k +
  question-shaping should cut the tokens-to-correct even where both arms eventually succeed.
  This is a control-plane-clean metric (token counts, no payloads).
- **TERTIARY — out-of-scope decline rate:** does the arm correctly decline the OOS tasks?
  (reuses the OOS archetype). A gate that blocks a garbage call is part of the edge.

### 5.5 Pre-registered decision rule

The edge claim ships in outward copy only if ALL hold, on the **painful** APIs:

1. **FCC delta ≥ +0.15 absolute** on ≥ 2 painful APIs (well outside the ~7–8%/task noise
   of small sets — so require a *consistent* multi-task swing, never a single flip), AND
2. **no FCC regression** on any painful API (Δ ≥ −0.05), AND
3. **tokens-to-first-correct-call: GECKO ≤ BARE** on ≥ 2 painful APIs (correct AND
   cheaper, or at least not more expensive), AND
4. on the **clean negative control**, GECKO delta ≥ −0.05 (Gecko must not *break* an API a
   bare agent already handles — the "never worse than the raw dump" invariant, extended to
   the bare-agent baseline).

### 5.6 The NULL result we accept (and its meaning)

> On well-documented specs the bare agent matches or beats GECKO (delta ≈ 0 or negative);
> the positive delta appears ONLY on spec-less/painful APIs. **This is a PASS of the
> thesis, not a failure of the experiment** — it precisely locates our value at the ICP
> (PRD §2: "the Nth *painful* API"). We ship the edge claim *scoped to painful APIs* and
> explicitly DON'T claim an edge on clean ones.

And the failure we must be willing to report:

> If the FCC delta is inside noise on the **painful** APIs too — Gecko does not measurably
> help even where we bet it would — the edge claim stays OUT of outward copy, and the
> finding routes to `staff-engineer`/`product-manager`: either the comprehension is not
> yet deep enough, or the wedge is governance/credentials (which stand on their own),
> not first-call-correct. Reporting this honestly is the point of pre-registration.

### 5.7 Cost & mode

Recorded/$0 wherever the API allows synthesis; live is the FINAL check on the two or three
selected APIs, not the debugger (Pattern B). Records are control-plane clean (booleans,
token counts, arg-shapes — never payloads, never raw values beyond the disambiguation
kind), reusing the `RunRecord` shape. Written to `private/` (strategy/numbers gitignored).

---

## 6. Build plan — the Pattern-B falsifier is deliverable #1

Strict order; every step names its $0 offline falsifier on the golden sets FIRST, and no
step touches `ingest`/`catalog`-core/`tools`/`caller` (escalate to `staff-engineer` if a
signal seems to need an engine-file change).

1. **FALSIFIER FIRST — the tier eval harness + `tier_labels.jsonl` (§2.8).** A frozen,
   sha256-pinned tier golden set spanning read/write/transfer across ≥ 2 specs (Nora/
   Stripe-subset/hand-authored). A pure `evaluate_tier(operations, labels) -> {precision,
   recall, confusion}` scorer, offline/$0. This can FALSIFY the classifier before a line
   of it ships in the live scorer: if L1+L2 cannot hit precision ≥ 0.95 @ recall ≥ 0.80 on
   the frozen set, the tier signal does NOT ship. This is the gatekeeper.
2. **Tier classifier as a pure function** (spec §2.3–2.4) behind `score_call`'s inputs —
   `software-engineer` implements; measured against harness #1. Ships only if #1 is green.
3. **Wire tier into `_op_risk`'s weighting** (§2.5) as `op.transfer`/`op.transfer_maybe`
   Reasons — additive, never categorical, never in `BLOCKING_SIGNALS`. Falsifier: existing
   risk/enforce tests stay green + a new test that tier alone never yields `block`.
4. **"Wrong op for intent" anomaly** (§3.1) — reuses `search_scored`; falsifier = it must
   NOT regress `oos_pass_rate` (1.00) on the retrieval golden sets. Measured-lift-gated;
   shelved below the surface-all gate if it shows no lift.
5. **The owed experiment harness** (§5) — extend the two-arm `fcc_eval` pattern to a BARE
   (docs-fed) vs GECKO comparison + the tokens-to-correct metric. Offline/recorded first
   on Pegana/Nora specs; live final check on the selected APIs. Runs in PARALLEL with
   Pegana WTP (decision #5, PRD).
6. **DEFERRED (V2, corpus-gated):** L4 distribution-anomaly + L5 corpus failure classes —
   only after `data-engineer`'s observed-first rows exist and `fcc_eval.lift_corpus > 0`.

Falsifier order restated: **#1 (tier eval) is the first artifact**; no classifier ships
before its frozen-set precision/recall clears the §2.6 targets.

---

## 7. Top decisions for the founder

1. **Ship the tier classifier at precision ≥ 0.95 / recall ≥ 0.80, tier-never-blocks-alone
   (rec: YES).** Depth beyond HTTP method is the moat input for governance, and it is
   offline-falsifiable now. The safety valve is that tier alone only *warns* (step_up); a
   BLOCK needs the explicit policy intersection (spec 4d). This bounds the "false transfer
   blocks a paying call" cost to the narrow over-cap case. One-way-ish: tier becomes the
   term the whole governance predicate keys on.

2. **Accept the depth ceiling at L3; corpus-gate L4/L5 (rec: YES).** "Anomalous args from a
   learned distribution" is where precision collapses at 18–26 ops — it fabricates
   thresholds and false-blocks paying calls. Hold it until the flywheel produces real
   ground truth (`data-engineer`) and `fcc_eval.lift_corpus` proves it. This is the honest
   answer to "how deep before precision collapses" — and it keeps us off a false moat.

3. **Pre-register the owed experiment WITH a clean negative control the bare agent can win
   (rec: YES).** The experiment is only credible if it can fail. We commit the API-
   selection criteria and the decision rule (FCC delta ≥ +0.15 on ≥ 2 painful APIs, GECKO
   not worse on the clean control) before running, and we accept the NULL (edge appears
   only on painful APIs) as a thesis PASS scoped to the ICP. No "correct-first-try vs the
   bare agent" claim ships before this reports (PRD decision #5). Needs the Nora spec (and/
   or a Stripe-subset fixture) committed — which also unblocks the tier ground truth.

---

## 8. Seams (named, not duplicated)

- **context-engineer (4a)** consumes this harness's verdicts to gate note-shipping — a
  SurfaceNote ships only on a measured FCC lift *this* eval reports. They shape what the
  agent reads; **we measure it.** The refusal-payload text an agent reads on a tier
  step_up is their schema; the tier *signal* that triggers it is ours.
- **software-engineer (4d)** implements every signal here behind `score_call`'s pure-inputs
  contract — semantics specified in this doc, code in `risk.py`. No new gate; `apply_gate`
  unchanged. Escalates to `staff-engineer` if a signal appears to need an engine-file edit.
- **data-engineer (4c)** supplies observed failure classes as eval ground truth once the
  flywheel runs — the input that unlocks L4/L5 and turns `fcc_eval.lift_corpus` from a
  structural zero into a measurable number.
- **Retrieval spec (07-09):** owns ranking quality + the BM25-remap; this doc consumes its
  `search_scored` output for the L3 anomaly and does not restate the gate.
