# Orquestra Program Surface — Implementation Roadmap (sprint plan)

**Date:** 2026-08-03
**Status:** Plan for review (founder gate before execution).
**Supersedes/extends:** `docs/specs/2026-07-30-program-surface-pda-graph.md`,
`docs/specs/2026-07-31-orquestra-provider-integration.md` (phases folded into the sprints below).

> **North star (founder):** point at *any* Solana program → catalog it under `orquestra/` →
> run a local **surfpool** TDD ($0) → (hosted) *point & simulate* → after the simulation
> passes, jump to real mainnet money. **"Simulate before you spend."**

---

## 0. Where this fits in our strategy

| Strategic frame | This work |
|---|---|
| **Three-pillar thesis** (comprehension + comprehension-native security + auth-day-one) | The **Program Surface** is the on-chain extension of pillar 1; anti-poisoning verdict → signer gate is pillar 2 on-chain; 1claw TEE keys = pillar 3 on-chain. |
| **Roadmap tiers** (V1 comprehension · V2 correlations+feedback · V3 trust) | Program Surface = **V1.5** (comprehension, extended to programs). The catalog + hosted simulate are new product surface *on* V1. Not V3 — we never sign. |
| **Compose, don't replace** | Gecko **comprehends + resolves**; **Orquestra builds/submits**; **1claw holds keys/signs**. We never build a tx builder, never a rail, never sign. |
| **Discovery is the bottleneck** ([[context7-catalog-discovery]]) | The **catalog** (from Orquestra `GET /api/projects`) is the discovery layer — "the hardest thing for agents is exploring the programs." |
| **Paid model** (devs free · providers flat per-API) | Hosted *point & simulate*, drift, catalog hosting = **provider-side** features → slot into **Depth / Portfolio** tiers ([[paid-model-and-roadmap]]). Local surfpool + OSS stays **free**. |
| **Commit to the painful case** | An agent that **acts on-chain with money** is THE painful case — messy accounts, un-derivable PDAs, one wrong seed = a reverted/expensive tx. Squarely our ICP. |
| **Design-partner proof** | **Berkay/Orquestra** = compose partner (validated: "fastest Meteora I've seen"). **Raff/Pegana** = design partner (validated: A 99/100 report) — the 3 Pegana use cases are the proof artifacts, NOT a WTP test. |

---

## 1. Building blocks (what must exist, + current state)

| # | Block | State |
|---|---|---|
| A | Config-driven program backbone (`provider_config.py`, packaged configs) | **shipped** |
| B | 4 programs derivation-proven on surfpool ($0) | **shipped** (Meteora/Pump/ORE/MetaDAO) |
| C | Servable surface (`gecko-orquestra --program <p>` → derive/graph/plan → Orquestra builder) | **Meteora only**; other 3 need slugs (now in hand) |
| D | New seed kinds — constant-pubkey, ATA derivation | **not built** (flagged follow-ons) |
| E | Resolver resolution — Gecko *reads* account data to fill dotted-path seeds (P2: "provide everything e2e") | **not built** |
| F | Executable plans — intent → full account set → Orquestra `/instructions/:name/build` | **Meteora `plan_swap` only** |
| G | surfpool **simulate** the full built tx (not just derive→verify) | **not built** (spec Phase 4) |
| H | Reusable **catalog** — `GET /api/projects` → browsable agent-ready program surfaces | **not built** (endpoint confirmed live) |
| I | Auto-comprehend a program from Orquestra `/pda`+`/instructions`+llms.txt merged with source-recovered gaps | **not built** (today hand-authored) |
| J | Hosted "point & simulate" | **not built** (stretch) |
| K | `1claw:` credential backend (TEE keys via resolver seam) · signing-gate | **signing-gate shipped**; backend not built (founder meets 1claw next wk) |
| L | Raff's 3 Gecko fixes (rerank · `gecko test --live` · report abs-path) | **not built** (small) |
| M | 3 Pegana use cases (proof for Raff) | UC#1 correlation **proven**; UC#2/#3 not built |

**Slugs (unblocks C/F):** Pump.fun `6i6q26bmm46b89xlxo1kv` · ORE `6alwvs9936laepljczqumb` · MetaDAO `krhmrxpy2fgwn3q0whic7`. Build base = `https://api.orquestra.dev/api/<slug>`.

**Key Orquestra endpoints we compose:** `GET /pda` (seed schemas) · `POST /pda/derive` · **`POST /program-accounts/query`** (read on-chain account data → resolves dotted-path seeds like `bonding_curve.creator`) · `POST /instructions/:name/build` (build the base58 tx) · `GET /api/projects` (the catalog).

---

## 2. The sprints

Each sprint ends in a **demoable deliverable**. Effort: S/M/L. Founder-gated items marked 🔒.

### Sprint 1 — Servable + polish *(foundation)* · S
**Goal:** all 4 programs servable; Raff's polish shipped; clean base.
- Wire the 3 slugs → `pumpfun`/`ore`/`metadao` become servable like Meteora (`get_program_graph`, `derive_pda`; execute URL set).
- Raff's 3 fixes: (1) intent-vs-keyword **rerank** in `search_capabilities` (Pegana `state` should beat `list_alerts`); (2) `gecko test --live`; (3) `gecko report` prints the absolute output path.
- **Ship Pegana UC#1** (peg-state × Jupiter route, joined on mint) as a packaged runnable demo for Raff.
**Deliverable:** 4 servable programs + the Raff-fix release + the first Pegana use case in his hands.
**Fit:** finishes Berkay's immediate ask; design-partner momentum.

### Sprint 2 — The full account set *(seed kinds + resolution)* · M
**Goal:** express *and* resolve every account a real instruction needs — the P2 "Gecko provides everything e2e."
- **Constant-pubkey seed kind** (pump `metadata`/`fee_config`, metadao `token_metadata`).
- **ATA derivation** (pump `associated_bonding_curve`, metadao vaults) — SPL `[owner, token_program, mint]` under the ATA program.
- **Resolver resolution:** Gecko reads account data (via Orquestra `POST /program-accounts/query`) + Anchor-decodes to fill dotted-path seeds (`bonding_curve.creator` → `creator_vault`, ORE `round`, metadao). Control-plane note: reads *public account metadata*, never stores it — invariant #1 holds; decode logic is the work.
**Deliverable:** Pump.fun's **complete `buy` account set** derives — including `creator_vault` via a live read — proven on surfpool.
**Fit:** unblocks executable plans; delivers the founder's "provide everything so the call runs e2e."

### Sprint 3 — Executable + simulate *(the runnable, verified call)* · M–L ✅ sim DONE (Delivery 1)
**Goal:** intent → complete runnable plan → Orquestra builds → surfpool **simulates it would land**.
- Executable intents (Pump.fun `buy`/`sell` as reference) that assemble the full account set + point at `/instructions/:name/build`.
- Generalize the surfpool harness to **`simulateTransaction`** the built tx (spec Phase 4) — $0, no signing, the "it lands" proof.
**Deliverable:** `plan_buy` on Pump.fun → Orquestra-built tx → surfpool sim passes. The reusable "derive → build → simulate" loop for any program.
**Fit:** the core of "simulate before you spend" (P3 local rung); the engine behind the flagship demo.
**Delivery 1 (done):** the `simulate → Receipt` engine landed — `gecko/rpc.py` (canonical
transport, layering fix) + `gecko/simulate.py` (the Receipt: land/no-land, categorical
`revert_class`, compute units, best-effort SOL delta). Path A = the generic `simulate` MCP
tool; Path B = `plan_buy`'s self-serve `simulate` recipe. `simulateTransaction` only, never
signs, Receipt returned never stored. See `docs/receipt.md`.

### Sprint 4 — The reusable Orquestra catalog *(discovery)* · L
**Goal:** point at Orquestra → a browsable catalog of **agent-ready** program surfaces; comprehend-on-demand.
- `gecko-orquestra catalog` — reads `GET /api/projects` (paginated) → lists programs (name, program_id, description).
- **Auto-comprehend on pick** (block I): a program's config is *generated* from Orquestra `/pda` + `/instructions` + llms.txt, **merged with our source-recovered gap seeds** (the roots Orquestra drops). This replaces hand-authoring — "the same way we did the Orquestra projects," now automatic.
- Cataloged under `orquestra/` so an agent (or a dev) lists → picks → comprehends → runs.
**Deliverable:** `gecko-orquestra catalog` lists all programs; picking one yields a runnable surface with our recovered seeds — no hand-authored config.
**Fit:** solves the agent-exploration bottleneck; generalizes the whole thing; the "catalog it behind orquestra/" vision.

### Sprint 5 — Flagship proof + hosted + custody *(the product)* · L
**Goal:** the composed demo, the hosted pipeline, and TEE custody.
- **Pegana UC#2 (flagship):** depeg signal (Pegana) → build an on-chain de-risk swap (Program Surface + Orquestra builder) → **anti-poisoning verdict gates the 1claw signer** → surfpool-simulated e2e ($0). *Pegana + Gecko + Orquestra + 1claw composing.*
- **Pegana UC#3:** autonomous peg-health monitor (unattended, auth-at-call-time).
- **Hosted "point & simulate"** (🔒 infra): a hosted surfpool-sim endpoint — point at a program + intent, get a simulated-safe result. After it passes → the **mainnet handoff (🔒 founder-signed only)**.
- **`1claw:` credential backend** (🔒 gated on the 1claw meeting): lease short-lived TEE keys via the resolver seam; inject at call time.
**Deliverable:** the 3-use-case Pegana pack for Raff + the hosted simulate pipeline + 1claw compose.
**Fit:** design-partner proof, compose partners live, P3 hosted north star, monetization (Depth/Portfolio).

---

## 3. Dependencies & founder inputs

- ✅ Orquestra project slugs (in hand).
- ❓ **Which intents matter most per program** (Sprint 3): Pump.fun `buy`/`sell`? ORE `mine`/`claim`? MetaDAO `fund`? — pick the reference intent per program.
- 🔒 **Mainnet go-live** stays founder-signed (never Claude) — the hosted pipeline hands over a simulated-verified command; the founder broadcasts.
- 🔒 **1claw** backend gated on the founder's meeting next week.
- ❓ **Hosted infra** (Sprint 5 J): do we host surfpool-sim, or keep local-only for now? (Local is the $0 default; hosted is the stretch.)

## 4. Risks & honesty

- **Resolver reads** cross toward the data plane — mitigated: we read *public account metadata* (getAccountInfo / program-accounts/query), decode in-memory, **never persist** (invariant #1 holds). Call it out in review.
- **Auto-comprehend quality** (Sprint 4): merging Orquestra's `/pda` with source-recovered seeds must stay **honest** — a seed we can't resolve is flagged (ResolverPdaSeedNode), never fabricated. The whole differentiator.
- **Hosted simulate** = real infra + cost; gate it behind proven local value.
- **Keep $0-local as the default** everywhere; hosted/live is the final rung, never the debugger (Pattern B).

## 5. Definition of done (per sprint = a thing we can show)

1. 4 servable programs + Raff fixes + Pegana UC#1.
2. Pump.fun full `buy` account set (incl. resolved `creator_vault`) on surfpool.
3. `plan_buy` → Orquestra-built tx → surfpool sim passes.
4. `gecko-orquestra catalog` → pick any program → runnable surface, auto-comprehended.
5. Pegana flagship (depeg → on-chain → 1claw-gated, simulated) + hosted point-&-simulate.

---

## Sequencing note
Sprints 1→3 are the **critical path to a runnable on-chain call** (and the flagship). Sprint 4 (catalog) can run **in parallel** after Sprint 2 (it depends on auto-comprehend, which shares the seed-kind work). Sprint 5 composes everything + is partly founder-gated (1claw, hosted, mainnet). Pegana use cases thread across: UC#1 in S1, UC#3 in S3–4, UC#2 (flagship) in S5.
