# V2 Technical Design Document — Auth Injection · Self-Healing Probing Loop · Non-Standard Ingestion

**Date:** 2026-07-13 · **Authors:** synthesized from `web3-engineer` (Component 1),
`staff-engineer` (Components 2 + 3b), `ai-ml-engineer` (Component 3a + Gorilla/RAT).
**Status:** roadmap TDD (design; no code changes yet).

---

## 0. Executive summary & strategic framing

This TDD is **on-thesis** ([[three-pillar-thesis]]): it doubles down on **comprehension
(intent→right call)**, **correlations/multi-call** (offline multi-step training), and
**auth from day one** — the three pillars — and it **does not resurrect the retired
correctness corpus** (measured ~0 lift over comprehension). Every component is checked
against invariant #1 (control plane, never data plane) and #4 (auth invisible to the agent).

**Recurring finding: this is refinement, not greenfield.** Most of the surface already
exists — a governance/identity spec (`docs/specs/2026-07-09-agent-governance-identity-authinjection-design.md`),
`gecko/identity.py` + `gecko/policy.py` + `gecko/entitlements.py`, the `access.py` seam
(`Signer`, `ResolvedSession`, `OAuth2Lifecycle`, `establish_session`), `gecko/login.py`
(identity keypair), recorded mode + `gecko/sample.py`, `gecko/risk.py` (tier classifier),
`gecko/enforce.py` (remediation), and `gecko/docs_reader/` (honesty-gated draft recovery).

**Gorilla / RAT verdict (up front, because it shapes everything):** *same paper, two doors —
take the comprehension door, refuse the corpus door.*
- **ADOPT (comprehension, model-agnostic, no training):** Gorilla's **eval methodology** —
  the *hallucination-vs-error* split and the **retriever-ceiling** result (RAT proves
  retrieval quality hard-caps generation quality). These sharpen `gecko/fcc_eval.py`. Gorilla
  is external, peer-reviewed validation of our measured +0.6–0.83 comprehension lift ("the
  lift is in the *surface*, not the model").
- **REFUSE (corpus, breaks model-agnosticism):** RAT's *mechanism* is fine-tuning on
  accumulated `(query, doc, call)` triples — **the retired corpus in a more expensive wrapper**.
- **"Train a small Gecko model": NO for V2.** Only defensible niche = correlated/multi-call
  planning; kept as an evidence-gated V3 hypothesis, only if a *measured* multi-call FCC gap
  survives good retrieval + a grounding prompt.

---

## 1. Component 1 — The Auth Injection Mechanism

### 1.1 Deployment decision (the central tension, resolved)
Targets the **local adapter** (`gecko serve --stdio`): the real provider key is resolved from
the OS keychain **in-process on the agent's host** and injected toward the provider directly.
**Gecko-cloud is never in the data path** — "bypass" preserved, invariant #1 holds. A cloud
proxy is a gated-later variant (inject-forward-**discard**, metadata-only audit) that reuses
the same exchange.

### 1.2 Three things, never confused
1. **Identity keypair** — ed25519, sealed once at `gecko login` (`login.py`, `IDENTITY_REF`).
   Control plane stores only the pubkey/salted-hash. Root of *who*.
2. **Session Token** — a short-lived, scoped **capability**, minted locally by signing a
   challenge with the identity keypair. The **only** thing the agent holds; carries **no**
   provider secret — a signed claim of `{sub, scope, ent, srf, iat, exp, nonce}`.
3. **Provider credential** — the real key. Keychain-sealed (`CredentialRef(api=<surface>)`),
   resolved at call time (`ResolvedSession`), **never** transits cloud/agent/tool-def.

### 1.3 The exact Session-Token exchange sequence
1. **Mint** (runner boot): load the sealed identity key; build the canonical mint message
   (`b"gecko.session.v1\n" + json.dumps(claims, sort_keys=True, separators=(",",":"))` —
   matching `x402_pay._canonical`); `sig = Signer(msg)` (ed25519, `access.py:36`). TTL ≤900s
   (≤300s for `transfer` scope); `secrets.token_hex(16)` nonce; **token plaintext lives only
   in the runner process; control plane stores `token_hash` only.**
2. **Carry**: in the local model the token *is* the runner's bound MCP session; a tool call
   arrives at `McpSurface.call_tool(name, args)` already in scope — the agent never handles a
   header. (Cloud model: `Authorization: Bearer <session_token>`.)
3. **Verify + scope-gate (BEFORE any credential is resolved)** — new `SessionToken.verify`
   beside `GovernedSession`: (a) ed25519 signature vs enrolled subject; (b) freshness
   (`exp>now`, unseen nonce); (c) **scope** — the op's comprehended tier (`risk.classify_tier`:
   read<write<transfer) must be covered by the token `scope` AND in `policy.allowed_operations`;
   `transfer` also checks `spend_cap`/`recipient_allowlist`. Over-scope → `enforce.apply_gate`
   refuses (`scope.not_allowed`) **before** the provider key is ever resolved.
4. **Resolve** the real credential from the keychain chain (`ResolvedSession.auth_headers()`,
   `credentials.default_resolver`) — per call, never stored on the instance, never logged.
5. **Inject — three wire shapes** (from the surface's control-plane auth mapping, never the
   value):
   - **Bearer** — `{header: f"Bearer {secret}"}`; optionally `OAuth2Lifecycle` first exchanges
     the sealed refresh token for a short-lived access token via the *provider's own* endpoint.
   - **x402** — on `402`, delegate settlement to the injected `FacilitatorClient`
     (`x402_pay.py`): `verify`→`settle`→opaque `Settlement.reference`, attached as `X-PAYMENT`.
     **Gecko signs nothing, holds no funds.** `X402_MODE=stub` default; live is founder-gated.
   - **HMAC** (SigV4-style) — the secret is the signing key; **only a per-request, time-bound
     signature goes on the wire** (canonical string = method\npath\nsorted-query\nsha256(body)\n
     X-Timestamp; `hmac_sha256`). A leaked signature is worthless off its one request.
6. **Forward** host-pinned (`caller.build_request(..., allowed_auth_hosts=anchor)`) — a poisoned
   `servers[].url` → `CallError` naming only the host, never the auth value.

### 1.4 The audit trail (metadata-only) — auditable for providers, zero payloads stored
A frozen `AuthAudit` dataclass whose field set **is** the schema — `ts, subject (identity
pubkey fingerprint), surface_id, operation_id, decision (allow|step_up|block|degraded_noauth),
scope, injected_scheme, cred_backend, token_exp, request_id, payment_ref?, reasons?` —
**forbids by construction** any key/token/signature/body/header-value/amount/recipient. Rides
the existing `events.assert_fields_allowlisted` fail-closed allowlist.

**The provider gets a full, joinable trail without Gecko touching a payload:** the adapter adds
a non-secret `X-Gecko-Request-Id: <request_id>` header toward the provider; the provider joins
*their own* logs (which include their own response — Gecko never needs it) to Gecko's
metadata-only `AuthAudit` stream on `request_id` → "who called what, when, under which policy
decision, at what scope." Optionally tamper-evident (sign `sha256(canonical(AuthAudit))` with
the identity keypair — the off-chain analog of `gecko-receipt`).

**Open item (→ staff):** real-time revocation of an in-flight token before its TTL (short TTL
bounds exposure; a registry-hash re-check cadence needs nailing).

---

## 2. Component 2 — The Self-Healing Probing Loop

Build as **one new call mode (`probe`) + two seams** (`gecko/sandbox.py`, a `query_docs` MCP
tool) with **zero engine changes** to `ingest/catalog/tools/caller`. Probe is *recorded mode
with a validation pre-gate and synthetic state side-effects* — it stays on the no-wire side of
the transport edge, so invariant #3 ("one code path, two modes") holds.

**Step 1 — Temporary proxy interception.** The agent points at the **local** `gecko serve`
with `mode="probe"` (same `McpSurface`); the request terminates in the local adapter, no wire.
This is already how `recorded` behaves — `probe` formalizes it as a first-class mode.

**Step 2 — Stateful synthetic validation.** `sandbox.evaluate(op, args, world)` runs three
gates: (a) **structural** (reuse `caller._missing_required`), (b) **schema** (reuse
`risk._schema_conformance`), (c) **state** (the new escrow/ledger check, §3). On failure it
**renders the API's own error contract** — a new `client._error_schema(op)` (sibling of
`_success_schema`, scanning `400/409/422/default`) fed to `sample.example_from_schema` →
`{"status": 422, "mode": "probe", "data": <API-error-shaped body>, "signals": [...],
"remediation": {...}}`. *A signature scanner can't emit **this API's** 422; Gecko can, because
it comprehended the error responses* — the comprehension-native differentiator.

**Step 3 — MCP-driven self-healing.** The agent reads `remediation` (`enforce.REMEDIATION`) and
calls a new **`query_docs`** MCP tool over the **virtualized docs** (control-plane only:
operation/param descriptions from `ingest.Operation`, the `catalog` index, and
`agentnative.build_artifacts` tools.md/llms.txt). *Metaphor only — no real filesystem mount; a
search over spec-derived artifacts, control-plane-safe by construction.* The agent rewrites its
args and retries the same probe call until a synthetic 200.

**Step 4 — Production handover (bypass).** Env flips (`GECKO_MODE=live`, the knob `testgen`
already reads). The **same** `call_tool` path runs; only the transport edge changes — `SimWorld`
synthesis is replaced by `caller.execute` + Component-1 auth injection, agent calls the real
data plane directly. **"Bypass" = no Gecko-cloud hop, not "no Gecko on the machine."**

**New canonical type:** `CallMode = Literal["recorded","live","probe"]` (single source of
truth). **Load-bearing:** `corpus.source_for_mode` maps `probe → "synthetic"` — probe self-heal
outcomes route to `synthetic.jsonl` and can **never** inflate the published observed FCC rate.

---

## 3. Component 3b — The ephemeral sandbox state (`SimWorld`)

New module **`gecko/sandbox.py`** (all probe logic here; the MCP/HTTP surfaces stay thin).

- **`SimWorld`** — `{balances: dict[opaque_key, Decimal], last_touched}`. Process-local,
  **in-memory, per-`session_id`**, never written to disk. Balance keys are a **hash** of the
  recipient/account arg (via `risk._extract_recipients`) or a `"self"` bucket — the store never
  holds the raw account string, only a fabricated number under an opaque key.
- **Sim-rules are auto-derived from comprehension** (invariant #2 — adding API #2 needs **zero**
  new sandbox code): a `transfer`/write op with a debit-shaped verb (`withdraw/send/swap`) +
  amount → `require balance≥amount; on success balance-=amount`; a credit-shaped verb
  (`deposit/mint/fund`) → `balance+=amount`; everything else → no state effect. All from the
  same `risk.classify_tier`/`_extract_amount`/`_extract_recipients` the security pillar uses.
- **The multi-step correlation falls out for free** (the frontier, proven offline):
  ```
  deposit(100)  → balance=100 → 200
  withdraw(150) → 150>100 → synthetic 422 "insufficient balance" + remediation
  withdraw(80)  → ok → balance=20 → 200
  ```
  The agent learns *deposit-before-withdraw* against fabricated state, no live call.
- **TTL + LRU-cap GC**; process restart wipes it (desirable — it's synthetic).
- **Control-plane-safe:** fabricated integers under opaque keys (numbers Gecko invented, not
  API responses); never persisted; **`sandbox.py` has no `corpus.record` call site.** Reading
  request args transiently is normal request handling — categorically different from invariant
  #1's ban on *persisting response payloads*.
- **Escape hatch (data, not code):** a surface may ship a declarative `sim_rules` block the
  sandbox interprets — deferred until a real API demands it (v1 = auto-derived only).

---

## 4. Component 3a — Beyond OpenAPI 3.x (non-standard / Web3 ingestion)

Generalize `gecko/docs_reader.from_docs` into a **source-adapter family**, each emitting the
same honesty-gated `DocsDraft{draft, review_notes, low_confidence}` contract and **terminating
in the existing `ingest.Operation` list** (so `tools/catalog/dense/fusion/caller/fcc_eval` are
untouched — invariant #2):

- Adapters: `openapi` (existing), `docs` (existing), `postman`, `graphql` (SDL→fields→params),
  `grpc` (`.proto`→methods; flag the HTTP/2+protobuf **wire** as a `software-engineer` follow-on),
  `webhook` (inbound-only, `x-callable:false`), `abi` (EVM/Solana ABI/IDL → surface here; the
  call/settlement path is `web3-engineer` + [[gecko-programs-solana-tier]]). Promote the existing
  `Transport` literal to the canonical enum module.
- **RAT-style retrieval over messy docs (LLM-assisted recovery):** chunk the doc, retrieve only
  the relevant chunks (reuse `catalog.py` BM25F over doc chunks), hand the model *only* those —
  RAT's "ground in the retrieved doc" applied to *recovery*. **The model proposes; the honesty
  gate disposes** — every recovered field lands `x-draft-confidence: low` + `x-review`; it can
  never silently promote to trusted.
- **Honesty gate = the trust boundary:** a draft with unresolved `x-review` notes is servable
  **only in recorded mode** ($0), never promoted to live-callable until a human/probe confirms
  and it passes a recorded-mode FCC threshold (`gecko test`). *We never present a fabricated
  call as first-call-correct.*
- **Drift-watch (control-plane-safe, NOT a corpus):** reuse `dense.py`'s per-op `embed_text_hash`
  as a ready-made drift signal — a changed hash = a changed surface. Re-ingest on a cadence,
  diff the normalized `Operation` list (new/removed ops, changed required params/enums), flag +
  re-run the recorded FCC gate. Stores **surface diffs, not call outcomes** — provider-side depth
  ("your API changed and here's the call that now breaks") without a single stored payload.

---

## 5. Gorilla / RAT roadmap adoptions (concrete, model-agnostic)

Into `gecko/fcc_eval.py`:
- **`hallucinated` metric** — picked-tool ∉ any presented tool name (trivially available). Makes
  the story quantitative in Gorilla's vocabulary: "question-shaping + auth-hiding drives
  tool-hallucination to ~0."
- **Two-stage eval** — report the fused retriever's **recall@k as the ceiling FIRST**, then FCC
  as the fraction the generator converts. If recall@k is the bottleneck, no description-tuning
  moves FCC (Gorilla's ceiling result).
- **RAT grounding prompt** — add explicit anti-hallucination framing ("if the goal isn't served
  by exactly one provided tool, decline"); A/B on the same Haiku arm.
- **Tune the retriever against downstream FCC, not recall in isolation** —
  `scripts/retrieval_arms_eval.py` selects the fusion/floor config maximizing end-to-end
  `fcc_rate` (selection as the model-agnostic surrogate for RAT's joint training).

**Skip:** shipping/fine-tuning a served model; adopting APIBench data (ML-model APIs = the
opposite of our painful-API ICP); building a public API catalog (APIZoo — no-public-catalog).

---

## 6. Invariant reconciliation (all components)

- **#1 (control plane only):** `AuthAudit` and probe outcomes forbid payloads/keys/tokens by
  construction; `SimWorld` is synthetic + ephemeral; drift-watch stores surface diffs;
  Gecko-cloud is never in the data path. ✓
- **#2 (engine is API-agnostic):** sandbox sim-rules and ingestion adapters are **data +
  interpreter**; adding API #2 touches no engine module. ✓
- **#3 (one code path, two modes):** `probe`/`live` diverge only at the transport edge;
  `stub_session` mints/resolves nothing. ✓
- **#4 (auth invisible):** the `auth_headers() -> dict[str,str]` seam is unchanged; the agent
  supplies token+intent, never a provider header. ✓
- **Never sign/broadcast a mainnet tx:** x402 signs/broadcasts nothing from Gecko; live
  settlement founder-gated. ✓
- **Corpus stays retired:** probe → `synthetic.jsonl`; no cross-customer "learn from probe
  failures" path (that is the retired corpus in a new hat — if it appears in review, stop). ✓

---

## 7. Build plan, reversibility, delegation

**Sequence (each phase offline-falsifiable first, Pattern B):**
1. **Gorilla/RAT eval sharpening** (cheap, high-signal) — `hallucinated` + retriever-ceiling in
   `fcc_eval.py`; the grounding-prompt A/B. *No new surface.* (`ai-ml-engineer`.)
2. **Component 2/3b — probe mode + `gecko/sandbox.py` + `query_docs`** — the self-healing loop +
   `SimWorld`. The correlations/multi-call frontier, proven offline. (`software-engineer` builds
   `sandbox.py`; `ai-ml-engineer` validates an agent self-heals a real painful API's multi-step
   flow offline.)
3. **Component 1 — Session-Token exchange + `AuthAudit`** — refine the governance spec; the
   mint/verify/inject/audit path. (`web3-engineer` + `software-engineer`.)
4. **Component 3a — ingestion adapters + drift-watch** — one adapter at a time, each with a
   committed fixture + offline test. (`ai-ml-engineer` + `software-engineer`; gRPC/webhook wire
   adapters deferred.)

**Reversibility — design carefully now (one-way):** the `probe` mode's agent-facing contract
(synthetic-error shape, `query_docs`); the Session-Token wire/claim format; the `AuthAudit`
schema + `X-Gecko-Request-Id`; the `{source}→draft-OpenAPI` honesty-gate contract; `probe →
synthetic` routing. **Iterate freely (two-way):** `SimWorld` internals, TTL/GC constants,
sim-rule heuristics, module layout, retriever tuning.

**Acceptance:** each component ships with its Pattern-B falsifier (offline, $0): the auth leak
suite (no key/token/sig/body in any audit/log/error), the scope-gate-before-resolve assertion,
the `deposit→withdraw` correlation proof, the `probe→synthetic` routing guard, the
`sandbox.py`-never-calls-`corpus.record` guard, and one ingestion-adapter fixture per source.
