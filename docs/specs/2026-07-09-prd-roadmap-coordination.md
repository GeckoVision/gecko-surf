# Gecko — PRD + Roadmap + Specs Coordination (2026-07-09)

Replaces the stale Notion "API comprehension layer" PRD. Built on the current thesis
(govern-your-agent). Coordinating architect: staff-engineer. Grounded in the shipped engine
(`risk.py`, `enforce.py`, `credentials.py`, host-pinned `caller.py`, `dense.py`/`fusion.py`
gated-off, `preflight.py`+corpus) and the 07-07/07-09 specs.

---

## PART 1 — THE PRD

### 1. Problem — the agent that fails silently and confidently
- Agents fail *plausibly*, not loudly: hallucinated params, premature state-mutating calls,
  drift-broken calls, poisoned-steer — "looks right while being wrong." Anchors: ToolFailBench
  (86.33% best, on CLEAN tasks), BFCL, NoisyToolBench.
- Stakes scale with the operation tier: confidently-wrong READ wastes tokens; WRITE corrupts
  state; TRANSFER drains a wallet. The tier gradient is why it's must-have, not nice-to-have.
- Credential posture makes it worse: multi-API agents hold N long-lived keys in plaintext
  env/dotfiles (the founder included) — each a bearer token bound to nothing.
- Non-claims (honest): moat is a bet (corpus unproven); WTP unproven; our edge over a bare
  agent on our own spec-less ICP is asserted-not-evidenced (owed experiment, §7).

### 2. Users & journeys
- **Primary: the multi-API agent developer** (center of gravity). Building+shipping an agent
  across many APIs → wants safety + autonomy + agent identity *while building*, not as
  compliance afterthought. Never pays — the wedge/community/corpus supply.
- Beats: `gecko auth set` (plaintext → keychain, shipped) → `gecko serve --registry <api>`
  (surface without a PyPI release) → agent calls first-call-correct → the gate blocks the
  steered/malformed/over-scope call before it fires → dev sees the refusal + *why*.
- **Secondary/paying: the API provider** with a living/paywalled/drifting API. Buys DEPTH
  (comprehension, drift-watch, agent-readiness verdict, anti-poisoning). Flat per-API, never
  take-rate.
- Anti-users: marketplace listings (pay.sh's ICP), hobbyists, blue-chip-only, anyone wanting
  us to hold funds or keys.

### 3. Product — one control point, two payoffs
- ONE control point between agent and API/chain. **Correctness** (right thing on the API;
  first-call-correct = proof point, not headline) + **Governance** (agent can't do a harmful
  thing — scoped/hidden creds, policy-gated read<write<transfer, spend caps, drain-prevention).
- The control point IS the shipped call path: comprehend → risk-score → gate
  (allow/step-up/block) → governed auth-injection → host-pinned direct call. <50ms, off-chain;
  the Pinocchio firewall (devnet) is the on-chain last resort for custody moves.
- Both payoffs from the SAME input — the parsed surface. You can't bolt governance onto a
  proxy that doesn't comprehend the API; you can't trust correctness from a converter that
  doesn't gate.
- Two modes, one code path: recorded ($0, offline, falsifiable) and live differ only at the
  transport edge. Governance is live-mode; recorded never resolves a credential.

### 4. Context + semantics — the differentiator (first-class)
- **Thesis:** depth of intent→call comprehension is what a converter, a lexical firewall, or
  the bare agent can't copy. Depth = per-op risk tiers derived from the parsed spec; semantic
  anomaly ("malformed *for this API*" / "wrong op for this intent"); policy auto-derivation
  (operators tune thresholds, never author rules); the SurfaceNote keyed
  `(surface_id, surface_rev, tool_name)`.
- The context contract is engineered, not dumped: JIT loading (breadcrumb → `get_capability`),
  untrusted-labeled notes, off-by-default re-injection, attention-budget discipline. One
  string = ranker index = agent reads = corpus stores (single source of truth).
- The corpus makes semantics compound: every observed failure class sharpens the risk signals
  and notes for the next agent on that surface. The moat mechanism — a bet with a falsifier
  (§7), not a fact.
- Boundary: semantic depth on the *API-consumption* path only. Not a general prompt-injection
  solver, not response-payload inspection (deliberate gap, invariant #1), not a build-time IDE
  scanner (safe-ai-skill's layer).

### 5. Scope — V1 / V2 / V3
- **V1 (correctness + local governance):** registry surfaces, local runner, keychain vault
  [shipped], risk+enforce gate [shipped], governance falsifier (AgentPolicy, transfer tier,
  spend caps), Agent-Skills emit, `get_capability` + tokenizer fix + eval harness.
- **V2 (corpus + identity):** observed-first capture via the runner, SurfaceNote loop
  (FCC-gated), SessionIdentity + GovernedSession, BM25/dense behind the >50-op gate, multi-
  surface at scale.
- **V3 (trust + on-chain mainnet):** response-sanity/verification (deferred on purpose),
  mainnet firewall (founder-run), scoped-mint (Keycard-real) when a provider offers OAuth/STS.
- Discipline: anything that makes us a rail, marketplace, or public catalog is out at every tier.

### 6. Business model & competitive boundaries
- Devs never pay; providers pay flat per-API for depth. Never usage-metered, never take-rate.
- Compose: context-hub = complement (adopt the untrusted-annotation *pattern*, emit
  Agent-Skills, never publish to its registry); safe-ai-skill = complement at the build-time
  layer (**never** position as a generic "agent firewall"); Keycard = the identity standard to
  compose at the auth seam; x402/pay.sh = the rail/catalog we consume.
- Real threat (honest): not providers improving docs (a treadmill) — a **model runtime
  building the corpus natively.** Counter: neutrality, provider-ownership, long-tail.

### 7. Success metrics — including the honest unknowns
- **FCC** per surface (offline harness) — published = observed only, never self-reported.
- **Gate efficacy:** steered over-scope/over-cap transfer blocked in the $0 falsifier; false-
  block rate on paying calls ~0.
- **The owed experiment:** Gecko vs a bare agent on a spec-less painful API — FCC delta +
  tokens-to-first-correct-call. Until run, the edge claim stays OUT of outward copy.
- **WTP gate (the decider):** one provider paying flat per-API (Pegana warm, Nora candidate).
  Dev-side proxy: `gecko auth`/serve activation + retention (telemetry now shipping).

### 8. Non-goals & invariants
- Control plane only — never store payloads/user-data/secrets. Provider keys never transit
  Gecko (by architecture). Auth invisible to the agent. Gecko never holds/moves funds; issuer
  runs the hook; Claude never signs/broadcasts.
- Engine API-agnostic: API #N = data + the `auth_headers()` seam. One code path recorded/live.
- No public catalog, no free-text telemetry (closed vocab + counts + ranks only), no re-listing.

---

## PART 2 — BUILD vs COMPOSE vs DEFER

| Capability | Verdict | Detail |
|---|---|---|
| Multi-API surface (ingest→catalog→tools→caller, registry) | **BUILD — core** | The comprehension engine IS the product. |
| Credential vault | **BUILT (shipped)** | chain keychain→command→env, `gecko auth`, leak suite. Open: keyring base-dep vs extra (decision #4). |
| Agent identity | **BUILD thin + COMPOSE Keycard** | Build `SessionIdentity`+policy binding locally (comprehension-derived policy is ours); compose Keycard as the *standard* when it hardens; token mint pass-through until revocation demanded. |
| Governed auth-injection | **BUILD** | Seam (`auth_headers()`) ours + shipped; `GovernedSession` = one adapter; credential-selection-by-policy is moat (needs comprehension). |
| Spend caps | **BUILD off-chain; on-chain DEVNET-honest** | Off-chain categorical block in `risk.py`; on-chain firewall = backstop for opaque custody, DEVNET, no mainnet claim until founder-run. |
| Drain-prevention | **BUILD both tiers, honest maturity** | Off-chain gate SHIPPED; on-chain Pinocchio DEVNET; `custody-probe` red-team = falsifier. |
| Context/semantic depth | **BUILD — THE moat surface** | Per-op tier, semantic anomaly, intent→call grounding, policy auto-derivation. Uncopyable input to both payoffs. |
| Correctness corpus / memory | **BUILD storage+capture; COMPOSE the pattern** | Observed-first via the runner (status/class only, payload-free); adopt context-hub's untrusted-annotation *pattern*, never their store. |
| Drift-watch | **BUILD** | `preflight.py` drift check → surface_rev diff → breaking-change verdict; the provider-paid spine. Mintlify-style report. |
| Retrieval | **BUILD-lite, gated** | Tokenizer fix now; BM25 (OpenAPI-remapped) behind >50-op gate; dense/fusion stays OFF until BM25 leaves recall@3<0.8. Never rebuild chub's ranker. |
| Build-time/IDE firewall | **COMPOSE (safe-ai-skill)** | Different layer; coexist; never "generic agent firewall." |
| Payments/settlement | **COMPOSE (x402/pay.sh)** | Consume; never the rail. |
| Docs/interop emit | **BUILD emit, COMPOSE format** | `agentnative.py` emits Agent-Skills `SKILL.md` (our content, chub's format), LOCAL/BYOD. |
| Hosted BYOK / TEE / Gecko-run OAuth mint | **DEFER (V-next)** | Gate: demonstrated hosted demand + a provider token endpoint. |
| Response verification | **DEFER (V3)** | Deliberate gap (invariant #1). |

---

## PART 3 — ROADMAP  ([S] shipped · [D] devnet · [T] thesis)

- **Phase 0 (now, ungated):** `get_capability` [T→S], camelCase tokenizer fix [T→S], **offline
  eval harness** (the gatekeeper — build first, Pattern B) [T→S]; governance **Phase-1
  falsifier** (`AgentPolicy` + transfer-tier + spend-cap blocking signals in `risk.py`, $0/no-
  network, falsifies "steered over-scope/over-cap transfer blocked before it fires") [T→S].
  Baseline shipped: vault, risk+enforce gate, anti-poison+host-pin, Preflight+corpus vocab,
  dense/fusion built-OFF.
- **Phase 1 (gate: Phase-0 falsifiers green):** registry local-execution build-out (FixtureRegistry
  offline first) [T→S]; Agent-Skills `SKILL.md` emit + SurfaceNote authored in that shape [T];
  **the owed experiment** (Gecko vs bare agent, spec-less painful API) in PARALLEL with Pegana
  WTP — evidence + revenue discovery concurrently.
- **Phase 2 (gate: FCC eval shows a note fixes a call):** observed-first capture (runner posts
  `{surface, rev, failure_class}`, closed vocab) [T]; SurfaceNote loop (off-by-default, lands
  only where the harness shows lift) [T]; `SessionIdentity` + anon free-tier identity + leak
  suite (token derivation pass-through until revocation demanded) [T]; `GovernedSession` [T].
- **Phase 3 (gates: >50 usable ops; WTP signal):** BM25 arm (currently 18/26 ops → gate un-met)
  [S-OFF]; runner writes verdict-hash to `gecko-receipt`/denied-set to `gecko-firewall` on DEVNET
  (founder-run) [D]; provider-paid drift-watch iff WTP signal.
- **Phase 4 (V-next, each own gate):** Keycard-real scoped mint (first OAuth provider), mainnet
  firewall (founder-run), hosted passthrough/TEE (demand-gated), compose-CONSUME chub docs
  (measured-lift-gated, default don't).

---

## PART 4 — THE FOUR SPECIALIST BRIEFS

Shared: engine files untouched; control-plane invariants; Pattern-B falsifier is the FIRST
deliverable of every spec; closed-vocab telemetry only.

**4a · context-engineer — "the context contract" (centerpiece half 1).** Scope: everything the
agent *reads* — the SurfaceNote schema + Agent-Skills authoring shape (one-string/four-consumers),
JIT injection policy (breadcrumb → `get_capability` body load), untrusted-input labeling,
validated-at-write (path allowlist + `looks_like_secret_value`), attention-budget accounting, the
`SKILL.md` structure, and the **refusal payload** shape (what a blocked agent reads so it self-
corrects). Question: *minimum context, loaded when, that maximizes FCC per token — without becoming
an injection vector or a payload sink?* Seams: ai-ml owns measurement (a note ships only on measured
FCC lift); data owns persistence keying; software owns emit plumbing. Return: frozen SurfaceNote
schema + injection-policy state machine, token-budget claim quantified vs the −77/−89% baseline.

**4b · ai-ml-engineer — "semantic depth" (centerpiece half 2).** Scope: (1) per-op **risk-tier
derivation** from the parsed spec (transfer/spend detection beyond HTTP method — param semantics,
amount/recipient shapes, path/operationId semantics; precision target because a false transfer-tier
blocks a paying call); (2) **semantic anomaly** signals feeding `score_call`'s pure interface;
(3) **intent→call grounding** quality (golden sets, recall@3, BM25-remap for the gate); (4) the
**pre-registered eval protocol** for the owed experiment (Gecko vs bare agent, spec-less painful
API, accepted null). Question: *how deep can comprehension-derived semantics go before precision
collapses — and what measured delta over a bare agent / lexical baseline does each depth layer buy?*
Seams: context consumes harness verdicts; software implements signals behind `score_call` (no new
gate); data supplies observed failure classes as ground truth. Return: the transfer/spend-tier
classifier design (features, thresholds, fail-closed, precision target) + the pre-registered
experiment protocol (honest that the bare agent may win on well-documented specs — fine, our ICP is
spec-less/painful).

**4c · data-engineer — "the corpus, observed-first."** Scope: storage+capture — **observed-first**
(runner on-path = primary capture; wire status + failure class only, never a body; `reported` =
supplementary, quarantined, excluded from published FCC); corpus row schema at `~/.gecko/corpus/` +
the `/registry/feedback` endpoint (closed vocab, allowlist, size-capped); keying `surface_id+
surface_rev` (never session); `_classes.json` rollup; retention; and the **DB gate** (row volume /
cross-surface query trigger at which files→DB; which DB — Mongo already in stack). Question: *minimal
payload-free row that still teaches the next agent — and at what volume does file storage stop being
retrievable?* Seams: context defines the note (data stores/serves, never authors); ai-ml consumes
rows; software wires the runner hook + feedback route. Return: frozen corpus row schema + capture
transport (opt-in semantics, batching, exact posted body asserted byte-for-byte) + the DB-gate number.

**4d · software-engineer — "the concrete map + build order."** Scope: module-by-module plan —
Phase-0 (`get_capability`, tokenizer fix, eval harness), governance Phase 1 (`AgentPolicy`, two
blocking signals, fake-client falsifier), `identity.py` (new, <300 lines, leak suite),
`GovernedSession` in `access.py` (one adapter, seam-identity test), registry build (FixtureRegistry
first), the runner capture hook, the `agentnative.py` SKILL.md emit. Each phase names its $0 offline
falsifier FIRST + targeted pytest node ids. Question: *strict dependency-ordered sequence where every
phase ships a green offline falsifier before any wire work, and no phase touches
ingest/catalog-core/tools/caller?* Seams: implements ai-ml's signals behind `score_call`, context's
emit+refusal, data's capture hook; escalates to staff-engineer if any item forces an engine-file
change. Return: ordered build plan + per-phase falsifier + seam-identity test list.

---

## PART 5 — TOP 5 FOUNDER DECISIONS + RISKS

1. **Confirm observed-first corpus capture** (rec: YES). The runner on-path is the only payload-free
   trustworthy capture; `reported` stays supplementary, out of published FCC. One-way (sets corpus
   provenance; makes the registry runner load-bearing for V2 → runner adoption becomes the moat's
   bottleneck, risk 3).
2. **Spend-cap claim posture** (rec: ship off-chain caps as honest policy "we block the call we can
   see," on-chain = DEVNET backstop; do NOT hold the feature for mainnet; no "drain-prevention" in
   outward copy without the DEVNET qualifier until founder-run mainnet).
3. **SessionIdentity: shape now, token later** (rec: ship shape+policy binding; token derivation
   pass-through until a governance customer demands per-session revocation; Keycard = honest
   narrative until a provider offers a real token endpoint).
4. **`keyring` base-dep vs extra** (rec: **BASE** — the plaintext-key pain is the wedge; the safe
   path must be the zero-config path; dep-discipline yields here).
5. **Owed experiment vs WTP outreach** (rec: PARALLEL, not serial; but no "correct-first-try vs the
   bare agent" claim ships before the experiment reports).

**Risks (where the plan is weak):**
- **Front-loaded infrastructure, back-loaded evidence.** Governance+registry+corpus+compose all in
  flight before either decider (WTP, the edge experiment) has data. Mitigation = decision #5's
  parallelism, held strictly.
- **Governance drift toward "generic agent firewall"** (crowded; breaks safe-ai-skill coexistence).
  Discipline: every governance claim must trace to a comprehension-derived input; if a signal
  doesn't need the parsed surface, it's someone else's product.
- **The flywheel's cold-start moved, not solved.** Observed-first makes capture clean but makes
  corpus volume = runner adoption; devs-never-pay means distribution is the input. Treat runner
  activation/retention as a V2 gate metric, not vanity.
- **Static-PAT reality undercuts the identity story.** `mint_scoped` is pass-through everywhere
  today; keep the degraded-posture-per-surface documentation or Keycard positioning reads as vapor.
- **Spec sprawl** (9 specs in 3 days, 1 founder). The briefs are deliberately narrower than the
  specs they extend — resist re-opening settled ground (rejected verbs, no-public-catalog, frozen
  `search_capabilities` shape).

**DX note (don't over-index):** mattpocock/Mintlify discipline fits exactly two places — the emitted
`SKILL.md` (scannable, example-first) and the provider-facing drift-watch report (reads like a
Mintlify changelog, not a diff dump). Neither justifies a docs-platform build.
