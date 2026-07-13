# Self-Healing Probing Loop + SimWorld — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax. Source design: `docs/specs/2026-07-13-v2-tdd-auth-selfheal-ingest.md` §2, §3, §5.

**Goal:** Formalize $0 recorded mode into a first-class **`probe` mode**: an offline sandbox where an agent's malformed calls get the API's *own* synthetic error, self-heal via a `query_docs` tool, and — with an ephemeral `SimWorld` — learn **multi-step correlated workflows** (deposit→withdraw) without touching production. This is the correlations/multi-call frontier, proven offline.

**Architecture:** One new call mode + two seams (`gecko/sandbox.py`, a `query_docs` MCP tool), **zero engine changes** to `ingest/catalog/tools/caller`. Probe = recorded mode + a validation pre-gate + synthetic state side-effects, all on the **no-wire side of the transport edge**.

**Tech Stack:** existing `gecko/client.py`, `sample.py`, `caller.py`, `risk.py`, `enforce.py`, `mcp_server.py`, `corpus.py`, `fcc_eval.py`, `catalog.py`, `agentnative.py`; Python 3.11+/uv.

## Global Constraints (bind every task)

- **Invariant #1 (control plane, never data plane):** `SimWorld` holds only *fabricated* integers under *opaque* keys — never a real response payload, arg value, user datum, or secret; **never persisted** (in-memory, TTL-GC'd). `gecko/sandbox.py` must have **no `corpus.record` call site**.
- **The corpus stays RETIRED ([[three-pillar-thesis]]):** probe outcomes route `source="synthetic"` → `synthetic.jsonl`, structurally excluded from any published metric. **Do NOT add any cross-customer "learn from probe failures" path** — that is the retired corpus in a new hat.
- **Invariant #2 (engine is API-agnostic):** sim-rules are **auto-derived from comprehension** (`risk.classify_tier`/`_extract_amount`/`_extract_recipients`); adding API #2 to probing touches **only data**, never `ingest/catalog/tools/caller`.
- **Invariant #3 (one code path, two modes):** `probe`/`recorded`/`live` diverge only at the transport edge; `probe` treated like `recorded` for the auth/host guard (no wire → no injection).
- **Single source of truth for the mode type** (`.claude/rules/python.md`): one canonical `CallMode` Literal, imported everywhere; never redeclared.
- **TDD, failing-test-first; `uv run ruff format && ruff check --fix && mypy gecko` clean before every commit; targeted pytest node ids.**
- **One-way contracts to design carefully now:** the `probe` agent-facing shapes (synthetic-error body, `query_docs`), and `probe → synthetic` corpus routing. **Two-way (iterate freely):** `SimWorld` internals, TTL/GC constants, sim-rule heuristics.

---

## Phase 0 — Gorilla/RAT eval sharpening (measure before we build)

*Cheap, high-signal, no new surface. Gives us the metrics to validate the loop's value.*

### Task 0.1 — `hallucinated` metric in fcc_eval
**Files:** Modify `gecko/fcc_eval.py`; Test `tests/test_fcc_eval.py`.
- [ ] **Failing test:** a `RunRecord` where the model's picked tool ∉ the presented tool names has `hallucinated is True`; a real-but-wrong pick has `hallucinated is False`.
- [ ] Add `hallucinated: bool` to `RunRecord` (picked-name ∉ the arm's presented tool names — `raw_caller`/`gk_caller` keys are already in scope). Compute at scoring time.
- [ ] Add `hallucination_rate(records, arm)` (sibling of `fcc_rate`).
- [ ] **Gate:** the metric reads only tool NAMES (no arg value / payload) — control-plane clean.

### Task 0.2 — Report the retrieval ceiling first
**Files:** Modify `gecko/fcc_eval.py` (or `scripts/flywheel_eval.py` output).
- [ ] **Failing test:** `retrieval_recall_at_k(records, arm)` returns the fraction of tasks whose gold op was in the surfaced top-k (`retrieval_hit` is already captured per task).
- [ ] Print order becomes: **recall@k (ceiling) → FCC (converted) → hallucination-rate**, so a retrieval bottleneck is visible before any generation tuning.

*(The RAT grounding-prompt A/B is a prompt-only experiment, tracked separately — not a code task here.)*

---

## Phase 1 — The `probe` call mode (plumbing, engine-safe)

### Task 1.1 — Canonical `CallMode`
**Files:** Create `gecko/modes.py` (or add to an existing canonical module); Modify `gecko/client.py`, `gecko/corpus.py`, `gecko/testgen.py`; Test `tests/test_modes.py`.
- [ ] **Failing test:** `from gecko.modes import CallMode` exists as `Literal["recorded","live","probe"]`; `corpus.source_for_mode("probe") == "synthetic"`.
- [ ] Define `CallMode`; update `corpus._MODE_TO_SOURCE["probe"] = "synthetic"`; import the Literal everywhere `mode` is typed (no redeclaration).
- [ ] **Control-plane gate (failing test):** a probe outcome is written to `synthetic.jsonl`, never the main corpus.

### Task 1.2 — Route `probe` through the surface like recorded
**Files:** Modify `gecko/client.py` (`_effective_mode`, `call`), `gecko/mcp_server.py` (`call_tool`); Test `tests/test_probe_routing.py`.
- [ ] **Failing test:** `client.call(tool, args, mode="probe")` does NOT invoke `caller.execute` (assert via an injected transport that records calls — it stays 0) and does NOT inject auth.
- [ ] `_effective_mode` treats `probe` like `recorded` for the auth/host guard; `call` dispatches `probe` into `sandbox.evaluate` (Phase 2). `mcp_server.call_tool` threads the existing `session_id` through.

---

## Phase 2 — Stateful synthetic validation → the API's own error

### Task 2.1 — `_error_schema(op)` (the comprehension-native differentiator)
**Files:** Modify `gecko/client.py` (sibling of `_success_schema`); Test `tests/test_error_schema.py`.
- [ ] **Failing test:** for an op whose spec declares a `422`/`400`/`default` error response schema, `_error_schema(op)` returns that schema; `sample.example_from_schema(_error_schema(op))` yields a body shaped like *that API's* error.
- [ ] Implement `_error_schema` scanning `400/409/422/default` response schemas (mirror `_success_schema`).

### Task 2.2 — `gecko/sandbox.py::evaluate` — three gates → synthetic result
**Files:** Create `gecko/sandbox.py`; Test `tests/test_sandbox_evaluate.py`.
- [ ] **Failing test:** `evaluate(op, args, world)` with a missing required field returns `SimResult(status=422, mode="probe", data=<error-shaped>, signals=["schema.required"], remediation={...})` — **not** a raised `CallError`.
- [ ] Implement `evaluate`: gate (a) structural (reuse `caller._missing_required` logic as a *result*, not an exception), (b) schema (reuse `risk._schema_conformance`), (c) state (Phase 3). On failure, render via `_error_schema` + `sample.example_from_schema` + `enforce.REMEDIATION`; on success, synth 200 from `_success_schema`.
- [ ] **Gate:** `SimResult` carries `mode_note` marking it synthetic; the result dict has no real-payload field.

---

## Phase 3 — `SimWorld`: the ephemeral state (the correlations proof)

### Task 3.1 — `SimWorld` + `SimStore`
**Files:** `gecko/sandbox.py`; Test `tests/test_simworld.py`.
- [ ] **Failing test:** `SimStore.get_or_create(session_id)` returns a per-session `SimWorld`; two session ids never see each other's balances; a world past TTL is evicted; the session count is LRU-capped.
- [ ] Implement `SimWorld{balances: dict[str,Decimal], last_touched}` + `SimStore` (process-local, in-memory, TTL + LRU GC, never written to disk). Balance keys = a **hash** of the recipient/account arg (via `risk._extract_recipients`) or a `"self"` bucket.

### Task 3.2 — Comprehension-derived sim-rules + the multi-step correlation
**Files:** `gecko/sandbox.py`; Test `tests/test_simworld_correlation.py`.
- [ ] **Failing test (the frontier proof):**
  ```
  deposit(amount=100)  → status 200, balance["self"]==100
  withdraw(amount=150) → status 422, data error-shaped "insufficient", balance unchanged
  withdraw(amount=80)  → status 200, balance["self"]==20
  ```
- [ ] Implement rule derivation from `risk.classify_tier` + amount/verb shape: debit verbs (`withdraw/send/swap/debit`) → `require balance≥amount; on success balance-=amount`; credit verbs (`deposit/mint/fund/credit`) → `balance+=amount`; else no state effect.
- [ ] **Control-plane gate (failing test):** grep + behavioral assert that `gecko/sandbox.py` never calls `corpus.record`; `SimWorld` holds only `Decimal`s under opaque keys (no raw account string, no response payload).

---

## Phase 4 — MCP-driven self-healing: `query_docs`

### Task 4.1 — `query_docs` tool (control-plane-safe)
**Files:** Modify `gecko/mcp_server.py` (thin dispatch); logic in `gecko/sandbox.py` or a `catalog` search fn; Test `tests/test_query_docs.py`.
- [ ] **Failing test:** `query_docs(intent)` returns spec-derived artifacts only (operation/param descriptions from `ingest.Operation`, `catalog` hits, `agentnative.build_artifacts` snippets) + the relevant tool schema; assert the output contains **no** auth field, no `_invoke`, no payload.
- [ ] Implement as a sibling of `search_capabilities`. *Naming: `query_docs` (the "filesystem" framing is a metaphor — a search over spec-derived artifacts, never a real mount).*

---

## Phase 5 — Production handover + end-to-end validation

### Task 5.1 — probe → live is the same path
**Files:** Modify `gecko/client.py`/`gecko/mcp_server.py` as needed; Test `tests/test_probe_to_live.py`.
- [ ] **Failing test:** flipping `GECKO_MODE=live` runs the identical `call_tool` path with an injected transport; only the transport edge differs — `SimWorld` synthesis is replaced by `caller.execute` (+ auth injection, Component 1, when built). Assert the code path (not the outcome) is shared.
- [ ] Document in the served-surface help: **"bypass" = no Gecko-cloud hop, not "no Gecko on the machine."**

### Task 5.2 — Agent self-heals a real painful API's multi-step flow offline (validation, `ai-ml-engineer`)
**Files:** `scripts/selfheal_eval.py` (new, sibling of `flywheel_eval.py`); a golden multi-step task set.
- [ ] Build a small multi-step golden (e.g. a deposit→settle flow on a painful API fixture). Run an agent-in-loop against `mode="probe"`: it hits a synthetic error, calls `query_docs`, rewrites, retries, and completes the correlated sequence — measured with the Phase-0 metrics (does self-heal raise multi-step FCC; is hallucination ~0).
- [ ] **This is the frontier's go/no-go:** report whether the offline loop measurably teaches the correlated sequence. Honest either way (a thin lift is a real finding, per Pattern B).

---

## Acceptance
- `probe` is a first-class mode; probe outcomes route `synthetic`; `sandbox.py` never writes the corpus (gated by test).
- A malformed probe call returns the API's *own* synthetic error + remediation (not a raised exception).
- `SimWorld` proves the deposit→withdraw correlation offline; per-session isolation + TTL/GC hold.
- `query_docs` returns only control-plane-safe spec artifacts.
- `probe→live` shares one code path (invariant #3).
- Phase-0 metrics (`hallucinated`, retrieval-ceiling) live in `fcc_eval.py`.
- Phase-5 validation reports whether the offline self-heal measurably teaches a multi-step flow.

## Reversibility
- **One-way (design now):** the synthetic-error body shape, `query_docs` contract, `probe→synthetic` routing, `CallMode`.
- **Two-way (iterate):** `SimWorld` internals, TTL/GC/LRU constants, sim-rule heuristics, module layout.

## Delegation
`software-engineer` builds `gecko/sandbox.py` + the `client`/`mcp_server` wiring (sequentially — code agents tangle git). `ai-ml-engineer` owns Phase 0 (eval) + Phase 5.2 (validation that comprehension-derived rules + `query_docs` actually drive self-heal). `product-designer` on the agent-facing `query_docs` shape once the engine lands.
