# Roadmap — architecture gaps, docs comprehension, what's next (2026-07-13)

Synthesized from this session's evidence. Grounded in [[three-pillar-thesis]]: the moat is
**comprehension + correlations + auth-from-day-one, combined** (execution/integration/timing,
not a data moat). The correctness corpus is **retired** (measured ~0 lift). Everything below
serves the three pillars and stays control-plane-safe (invariant #1).

---

## 1. Architecture gaps (honest inventory)

| # | Gap | Severity | Status |
|---|---|---|---|
| G1 | **No multi-call / correlations engine** — Gecko nails the *single* first-call; it can't yet plan a *sequence* (call A → id → call B). This is the frontier and the harder-to-copy value. | **High** | self-healing loop plan (PR #123) in build; Phase 0 done |
| G2 | **Docs comprehension is narrow** — only OpenAPI 3.x + HTML-docs recovery. No GraphQL/gRPC/Postman/AsyncAPI/HAR/on-chain-ABI. The ICP (painful Web3 APIs) rarely ships clean OpenAPI. | **High** | designed (V2 TDD §3a), not built |
| G3 | **10 MB spec cap** blocks large real APIs (GitHub's ~14 MB spec fails). | Medium | needs chunked/streaming ingest for trusted sources |
| G4 | **Auth is bearer-only today** — the Session-Token → keypair/SIWS exchange (invisible + auditable) is designed (V2 TDD §1) but not built; in-flight-token revocation is open. | **High** | designed, not built |
| G5 | **We're semi-blind to real usage** — `gecko login` (client) exists but needs the `/registry/keys` deploy + the SIWS/keypair upgrade + an onboard-ping so we can attribute real devs vs crawlers (~90% of current traffic is bots). | Medium | client shipped; server + upgrade pending |
| G6 | **No live-outcome feedback path** — Gecko may not see how the agent's call fared in production (the "proxy vs direct" tension). *But* the corpus is retired, so this matters less as a moat; it still matters for **drift-watch** (provider-side value). | Low-Med | intentionally deferred |
| G7 | **No V3 response-side verification** — "is this response sane / is sensitive data leaking outbound" — designed-outline only. | Low | V3, deferred |
| G8 | **No multi-call eval harness** — we can't yet *measure* correlations quality (the thing G1 builds). | Medium | Phase 5 of the loop plan |

**The meta-gap (strategic):** no compounding data moat. Defense = **ship faster + broader than anyone can integrate the three pillars, and own the "comprehension-native security + auth" narrative.** Won by execution, not accretion.

---

## 2. Docs comprehension — the plan for *good* comprehension

This is the load-bearing pillar. Today: `ingest.py` (OpenAPI 3.x) + `docs_reader.from_docs`
(HTML → honesty-gated draft OpenAPI, REST + JSON-RPC, `x-review`/`x-draft-confidence`). Good
bones; too narrow. The upgrade, in priority order:

**D1 — Format breadth (the source-adapter family).** Generalize `docs_reader` so every source
emits the same honesty-gated `DocsDraft` → the existing `Operation` contract (engine untouched):
- **HAR / traffic capture** *(highest leverage for chaotic APIs)* — a dev pastes a browser HAR
  or a set of `curl`s; Gecko recovers the surface **from real request/response *shape*** (never
  the values — invariant #1) when there are no docs at all. This is how you comprehend the
  truly-undocumented Web3 API.
- **Postman collection** — deterministic method/URL, low-confidence descriptions.
- **GraphQL SDL** — introspection → one op per query/mutation field; flag nested-input-object
  and union return ambiguity `low`.
- **gRPC `.proto`** — deterministic method/message parse (surface now; the HTTP/2+protobuf
  *wire* adapter is a `software-engineer` follow-on).
- **AsyncAPI / webhooks** — inbound-only, `x-callable:false` (comprehend for understanding).
- **On-chain ABI / Solana IDL** — surface here; the call/settlement path is `web3-engineer` +
  [[gecko-programs-solana-tier]].

**D2 — RAT-style retrieval over messy docs** (from the Gorilla verdict). For unstructured
prose, chunk the doc, retrieve only the relevant chunks (reuse `catalog.py` BM25F over chunks),
hand the model *only* those, and land every recovered field `x-draft-confidence: low` +
`x-review`. Grounding cuts hallucinated endpoints at *ingest* time. Model proposes, honesty gate
disposes.

**D3 — Confidence-gated live promotion** (the trust boundary). A recovered surface with
unresolved `x-review` notes is servable **only in recorded mode** ($0) until it passes a
recorded-mode FCC threshold (`gecko test`) + human/probe confirmation. *We never present a
fabricated call as first-call-correct.*

**D4 — Drift-watch** (control-plane-safe, NOT a corpus). Reuse `dense.py`'s per-op
`embed_text_hash` as a ready-made drift signal: re-ingest on a cadence, diff the normalized
`Operation` list (new/removed ops, changed required params/enums), flag + re-run the FCC gate.
Stores **surface diffs, never payloads** — provider-side depth ("your API changed, here's the
call that now breaks").

**D5 — Break the 10 MB cap** for trusted sources — chunked/streaming ingest (or per-domain
split specs, GitHub-style) so large real APIs comprehend.

**D6 — Sharper measurement** (Phase 0, done): `hallucinated` + `retrieval_recall_at_k` now in
`fcc_eval.py`. Next: the multi-call FCC harness (G8) so correlations quality is measurable.

---

## 3. What's next (sequenced)

1. **Finish the self-healing loop** (PR #123 Phases 1–5) — `probe` mode + `SimWorld` +
   `query_docs`. The correlations/multi-call frontier, offline-falsifiable. *In build.*
2. **Docs comprehension breadth** (D1–D2) — start with **HAR/traffic ingestion** (biggest ICP
   unlock) + **GraphQL** (common), each a committed fixture + offline test.
3. **Drift-watch** (D4) — cheap, invariant-safe, provider-side value; reuses `embed_text_hash`.
4. **Auth Session-Token exchange** (V2 TDD §1) — once `gecko login` server is deployed.
5. **V3 response verification** — deferred until a customer needs unlinkable/anomaly checks.

**Guardrails (do not violate):** engine stays API-agnostic (adapters = data + interpreter);
no payload/secret ever persists; the corpus stays retired (no "learn across customers" path);
recovered surfaces never claim first-call-correct without passing the recorded FCC gate.

**One-line:** *double down on comprehension breadth (any source → the same tools) + correlations
(multi-call, offline) + auth (invisible, auditable) — the three pillars, won by shipping.*
