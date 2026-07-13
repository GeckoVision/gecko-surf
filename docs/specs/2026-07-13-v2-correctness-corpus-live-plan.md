# V2 — Correctness corpus live (MongoDB + evidence-gated retrieval) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Take Gecko's V2 (multi-API + correctness path) from "scaffolded but off the live path" to live — **but prove the moat on one API before standing up any infrastructure.**

**Architecture:** Almost everything is already built and *measured*, gated off the live path on purpose. This plan (a) proves `fcc_eval.lift_corpus > 0` on one API from real observed rows, then (b) wires the built machinery on — evidence-gated retrieval flips, a MongoDB corpus tier behind the existing `corpus.record()` seam, and consent-gated cross-tenant pooling. Dense/rerank stay built-but-gated.

**Tech Stack:** Python 3.11+/uv; existing `gecko/corpus.py`, `catalog.py`, `dense.py`, `fusion.py`, `corrections.py`, `telemetry.py`, `events.py`, `fcc_eval.py`; MongoDB Atlas (already used by `events.py` + `dense.py`); Voyage `voyage-4-lite` (Atlas autoEmbed, already wired).

## Global Constraints (every task's requirements implicitly include these)

- **Invariant #1 (control plane, never data plane).** The corpus stores ONLY correctness METADATA — never a response payload, a param/path/body VALUE, a token, or user data. Preserve `corpus.py`'s structural guarantees: `outcome_from` cannot receive a body; the allowlist writer fails closed; append-only (no UPDATE path on raw rows); synthetic/reported segregate by path→collection.
- **Every phase ends with a CONTROL-PLANE GATE**: a *failing-test-first* that scans the store and asserts (1) every persisted key ∈ the allowlist, (2) no substring of any value/body/token appears, (3) `path_template` stays templated. A control-plane violation is a build break, not a review comment.
- **Evidence, not vibes.** Retrieval flips are per-surface, gated on a measured paired win (95% bootstrap CI lower bound > 0 via `scripts/dense_gate.py`) AND an FCC confirmation (`fcc_eval`). The golden set is go/no-go only, never used to tune `k` (RRF `k=60` is pre-registered in `fusion.py`).
- **Seam-injected + offline-default.** New backends sit behind an env seam (`GECKO_CORPUS_BACKEND`, default `jsonl`), mirroring `events.py` / `X402_MODE`; `pymongo` stays an optional extra; the full test suite stays offline via injected fakes (`set_corpus_sink_override`).
- **TDD, targeted pytest node ids, `uv run ruff format && ruff check --fix && mypy gecko` clean before every commit.**
- **Founder-gated:** any live/observed bootstrap run and any deploy of the egress endpoint is founder-run.

---

## Phase 1 — Prove the moat on ONE API (the gate everything hangs on)

*Rationale (staff-engineer): the flywheel is unproven. Do NOT build MongoDB/pooling/dense until `lift_corpus > 0` on real **observed** rows from one API. This phase either confirms the moat or falsifies it — before any infra spend.*

### Task 1.1 — Observed-capture smoke on our own hosted serve of API #1
**Files:** `gecko/http_server.py` (`_capture`, already wired), a served surface's `corpus_path`; NEW `scripts/bootstrap_corpus.py`.
- [ ] Confirm `http_server._capture → corpus.record(outcome_from(status=...))` writes allowlisted JSONL for a served surface (TxODDS or Pegana). Set `corpus_path`.
- [ ] `scripts/bootstrap_corpus.py`: founder-run a **live** pass (`mode=live → source=observed → call_outcomes`) against API #1, credentials from the existing Session/keychain seam.
- [ ] **Control-plane gate (failing test first):** `tests/test_corpus_controlplane.py` — after the run, scan the JSONL: assert every key ∈ `ALLOWED_KEYS`; grep for any param/body/token value substring → none; `path_template` contains `{`.

### Task 1.2 — Measure lift on that one API (the go/no-go)
**Files:** `gecko/fcc_eval.py` (`lift_corpus`), `gecko/corrections.py`, `gecko/telemetry.py` (`aggregate`).
- [ ] Build `index.json` (per-API corrections) from the observed rows; run `fcc_eval.lift_corpus` on API #1.
- [ ] **DECISION GATE:** if `lift_corpus > 0` (FCC with corpus > FCC without, over `n_runs` with variance reported) → the flywheel runs; proceed to Phase 2+. If `≤ 0` → **STOP and escalate** (`ai-ml-engineer` + founder): the moat is falsified as designed; do not build the MongoDB/pooling tiers on an unproven asset. Record the result in `private/`.

*Honest ceiling: one API is plumbing-validation, not defensibility. This proves the flywheel **runs**, not that it's a moat.*

---

## Phase 2 — Retrieval: flip what's earned, wire the corpus in (evidence-gated)

*Rationale (context-engineer): current evidence (`private/2026-07-09-retrieval-arms.md`) says flip **BM25F** at scale now (privy 159 ops), keep **dense/rerank gated** (the dense gate doesn't fire). This phase adds the per-surface switch + the corpus-informed ranking; dense/rerank stay built-but-gated (Phase 5).*

### Task 2.1 — Per-surface `retrieval_profile`
**Files:** `gecko/surfaces.py` (new per-surface record field), `gecko/client.py` (`search` dispatch).
- [ ] Add `retrieval_profile = {lexical: "overlap"|"bm25", dense: bool, rerank: bool, gated_at_rev: str, evidence_ref: str}`, keyed by `surface_id`, pinned to `surface_rev` (a spec revision re-arms the gate). Default `overlap`/all-false → **byte-identical to today**.
- [ ] `AgentApiClient.search` reads it to choose overlap / bm25 / hybrid / +rerank.
- [ ] **Gate test:** a default-profile surface returns identical results to pre-change (snapshot).

### Task 2.2 — Flip Increment L (BM25F) where the gate fires
**Files:** `gecko/catalog.py` (route live `search_scored` through `BM25Index` under profile), `scripts/retrieval_arms_eval.py`, `scripts/dense_gate.py`.
- [ ] Route the live lexical arm through the built `catalog.BM25Index` when `retrieval_profile.lexical == "bm25"`; keep the 0/97 never-empty fallback + lexical-anchored OOS floor identical.
- [ ] Run the gate harness per surface: flip BM25F only where scale (>50 ops) AND lexical recall@3 < 0.80 AND a paired-CI win AND FCC non-regression. **privy qualifies; txodds/pegana do not** — verify, don't assume.
- [ ] **Gate test:** flipping a non-qualifying surface is refused by the harness; a flipped surface's FCC ≥ pre-flip.

### Task 2.3 — Corpus-informed ranking (reliability prior + correction enrichment)
**Files:** `gecko/telemetry.py` (per-operation reliability aggregate, observed-only), `gecko/fusion.py` (reliability as a third RRF list), `gecko/corrections.py` + `gecko/client.py` (`enrich_with_corrections` in the build path).
- [ ] Extend `telemetry.aggregate` to emit `(surface_id, operation_id) → (fcc_rate, n_observed, dominant_error_class)` from **observed** rows only.
- [ ] Apply as a **third RRF list** (reliability-ordered) in `search_hybrid_scored`, gated on `n_observed ≥ 30` (fail to neutral). Never lets a high-reliability op leap the OOS floor.
- [ ] Wire `enrich_with_corrections` into `AgentApiClient.__init__` (pinned per `surface_id`); every injected note passes `enrich.safe_blurb` fail-closed.
- [ ] **Control-plane test:** the reliability aggregate reads only `first_call_correct`/`error_class` (no value); a poisoned correction is dropped, never smuggled.

---

## Phase 3 — MongoDB corpus tier (behind the existing `record()` seam)

*Rationale (data-engineer): mirror the proven `events.py` Mongo seam exactly. Only build this once Phase 1 confirms lift and volume justifies a hosted tier (still far below the ~50-surface DB gate — this is a hosted aggregation convenience, not a scale necessity).*

### Task 3.1 — The sink seam (mirror `events.py`; don't weaken the choke)
**Files:** `gecko/corpus.py` (`record`/`record_adversarial` route through a backend), NEW `tests/test_corpus_mongo.py`.
- [ ] Add `_corpus_backend()` (`GECKO_CORPUS_BACKEND`, default `jsonl`), `_corpus_collection()` (`lru_cache`, lazy `pymongo`, 2 s timeout, `None` when `MONGODB_URI`/pymongo absent → falls back to JSONL), `set_corpus_sink_override()` (test seam). `to_record()`/allowlist choke runs **before** the sink is resolved — `CorpusError` still surfaces on any backend. Signatures unchanged → zero call-site churn.
- [ ] **Gate test (fake sink):** `CorpusError` still raises; `synthetic` routes to `synthetic_outcomes`, `observed`/`reported` to `call_outcomes`; the suite stays offline under the default backend.

### Task 3.2 — Collections + the three append-only walls
**Files:** `scripts/corpus_mongo_setup.py` (Atlas DDL), `gecko/telemetry.py` (Mongo-backed `aggregate` = observed-only `find`).
- [ ] Collections in `gecko_corpus`: `call_outcomes`, `synthetic_outcomes`, `adversarial_outcomes`, `corrections`, `priors`. Segregation is by collection (fails closed for a naive reader).
- [ ] **Three walls:** app = insert-only (no update path reachable from `record()`); server = `$jsonSchema` `additionalProperties:false` + `required` = `ALLOWED_KEYS` (+ `expire_at`) with `enum` from the closed frozensets, `validationAction:"error"`; auth = writer role holds `insert` only. TTL index on `expire_at` for raw rows; upsert allowed ONLY on derived `corrections`/`priors`.
- [ ] Indexes: `{surface_id,operation_id,error_class}` (§4a), `{method,error_class}` (§4b), unique keys on the derived collections.
- [ ] **Gate test:** an insert with an extra key is rejected server-side; an `update` on `call_outcomes` is denied by role; `telemetry.aggregate` over Mongo matches the JSONL aggregate for the same rows.

### Task 3.3 — Backfill our accumulated JSONL → Mongo
**Files:** NEW `scripts/corpus_migrate_jsonl.py`.
- [ ] Read every accumulated `corpus/*.jsonl` + `synthetic.jsonl`, **re-validate each line through `to_record`/`assert_allowlisted`** (fail-closed drops legacy bad rows), `insert_many` by `source`.
- [ ] **Gate test:** a crafted bad legacy row is dropped, not inserted.

---

## Phase 4 — Tenancy egress / cross-customer pooling (consent-gated)

*Rationale (staff + data): the network-effect unlock the spec deferred. Safe to pool because the contributed unit is a closed-vocabulary fact about the API's shape, not customer data. Blocking-before-external-contributors: reconcile the wire-format divergence.*

### Task 4.1 — Reconcile the `/registry/feedback` wire format (do this FIRST — one-way contract)
**Files:** `gecko/registry/api.py` (`_feedback`), `gecko/corpus.py` / `gecko/preflight_corpus.py` (single-source the vocabulary).
- [ ] The built endpoint accepts `{surface, surface_rev, classes}` (preflight `CLASSES`); the observed-first spec specifies `{items:[{error_class, surface_id, surface_rev}], v:1}` (`corpus.ERROR_CLASSES`). **Pick one** before any external machine posts: adopt `{items, v}` + reconcile `ERROR_CLASSES` vs preflight `CLASSES` into ONE source of truth. Add the `^[a-f0-9]{8,64}$` `surface_rev` guard + size-cap-refuse-whole.
- [ ] **Gate test:** an off-vocabulary class is refused; an oversized batch is refused whole (never truncated).

### Task 4.2 — Consent + the egress job
**Files:** NEW `scripts/contribute_corpus.py`; `gecko/corpus.py` (`local→contributed` upgrade helper); `consent` collection.
- [ ] `gecko serve --report-failures` (default OFF; `GECKO_REPORT_FAILURES=1`) + a `consent` collection keyed `{tenant, surface_id}` (per-surface opt-in). Local capture is unconditional and never egresses; egress is a separate governed job.
- [ ] `contribute_corpus.py`: find consented `observed` rows → **re-run `assert_allowlisted` at the boundary** → strip tenant identity (contributed row carries `surface_id`/`operation_id`/metadata only, no `install_id`/`tenant_id`) → `tenancy=contributed` → dedupe-insert into `gecko_corpus_pool` (unique `_dedupe_key`).
- [ ] **Gate test:** a `local`-only row and a non-consented surface never appear in the pool; contributed rows carry no tenant id; `corrections_for(surface_id)` retrieval key has no session/request/tenant id (cross-request reuse is structural).

---

## Phase 5 — Dense + rerank (built-but-gated; flip only when earned)

*Rationale (context-engineer): hold off-path until a surface clears the dense gate + FCC. Escalate the "has H cleared its bar" call to `ai-ml-engineer`.*

### Task 5.1 — Dense (Increment H) per-surface flip
**Files:** `gecko/client.py` (`search_hybrid_scored`, built), `gecko/dense.py` (built), `scripts/dense_gate.py`.
- [ ] Populate `gecko_catalog.catalog_ops` (Atlas vectorSearch, `surface_id`/`surface_rev` filter) at ingest regardless; flip retrieval to hybrid (RRF of dense + BM25) ONLY per-surface when the dense gate CI + FCC clear. Today: **no surface qualifies** — ship populate-only.
- [ ] **Gate test:** a `catalog_ops` doc's text fields derive only from the spec surface — a property test asserts NO field derives from a `CallOutcome` (the two data classes never cross).

### Task 5.2 — Rerank (Increment R) — the one genuinely new build
**Files:** NEW `gecko/rerank.py` (`Reranker` seam + `VoyageReranker`, SDK isolated like `HaikuEnricher`).
- [ ] `Reranker(Protocol).rerank(query, candidates, top_k)`; concrete `VoyageReranker` (`rerank-2.5`). Compose over the fused top-N pool; inherit the OOS floor (rerank re-orders, never promotes across the floor). Gate per-surface on recall@1/MRR shortfall + FCC + a *smaller-limit-at-equal-FCC* token check.
- [ ] **Test:** offline with a fake reranker; the SDK never reaches `catalog`/`client`.

### Task 5.3 — Cross-surface catalog ranking fix
**Files:** `gecko/catalog_mcp.py` (`search_capabilities`).
- [ ] Replace the raw-score merge (incomparable across corpora) with **RRF-over-ranks** (reuse `fusion.rrf_fuse`), so cross-surface ranking is scale-invariant. Namespacing by `(surface_id, tool_name)`.

---

## Acceptance (V2 "live")
- Phase 1 gate green (`lift_corpus > 0` on API #1 from observed rows) — else V2 halts by design.
- BM25F flipped on every qualifying surface; corpus reliability prior + corrections enrichment live; all default surfaces byte-identical.
- MongoDB corpus tier live behind the seam; three walls enforced; backfill done; every control-plane gate green.
- Egress consent-gated + wire-format frozen; our own 4 surfaces contributed as the first pool content.
- Dense/rerank populate-and-benched, flip-when-earned; cross-surface RRF fix shipped.

## Companion: V3 spec (start now, design-level)
Write `docs/specs/2026-07-13-v3-response-verification-spec.md` from the staff-engineer outline: **response-side verification, verdict-only, never a stored response.** `response_check.py` at the transport edge → a categorical `ResponseOutcome` (`response_conformant | schema_violation | error_shaped | anomalous | empty`) + intent-conformance (`intent_match|ambiguous|mismatch`) + the consumption-**exfiltration** case folded into `AdversarialOutcome` family-A (`leaked` + `leak_sink` channel-name, never the value). Non-goals: no stored response, no DLP product, no provider-reputation scoring. Gated behind Phase 1 (the `anomalous` signal needs observed rows).
