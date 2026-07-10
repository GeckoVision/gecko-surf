# Gecko × context-hub — COMPOSE build plan (not adopt) — 2026-07-09

**Founder reframe:** not *adopt* (absorb context-hub's features into Gecko) but
**compose** (Gecko and context-hub interoperate as distinct layers, each improving the
whole) — *so we improve what we have*. This is on-thesis: Gecko "composes on top of,
never rebuilds" (CLAUDE.md — we compose MCP / x402 / pay.sh; we never re-list as a
provider). This plan reshapes the four `context-hub-adoption-*` specs accordingly and
ties everything to the tool.

Supersedes the framing (not the technical content) of
`2026-07-09-context-hub-adoption.md`.

## Two threads, kept distinct

1. **IMPROVE OUR TOOL** — our own upgrades, *informed by* context-hub's ideas but built
   as Gecko getting better (these are the moat; not shared with chub).
2. **COMPOSE WITH context-hub** — interoperate via a shared contract; never reimplement
   what chub already does well, never publish into its public registry.

The line between them is the test: *does this make Gecko's own comprehension/corpus
better (thread 1), or does it let Gecko and chub strengthen the same agent together
(thread 2)?*

## The composition model (thread 2)

- **Layers:** chub = "what to know" (hand-authored prose, popular libraries); Gecko =
  "make it callable, first try" (machine-comprehended tools, the painful long-tail).
  Inverse coverage — they don't overlap, they complete each other.
- **The interop contract = the Agent-Skills spec** (YAML-frontmatter `SKILL.md`/`DOC.md`).
  This is the tie. chub already speaks it; the agent runtime already understands it;
  Gecko learns to emit and consume it.
- **Three composition directions:**
  - **Gecko → ecosystem (PRODUCE):** Gecko comprehends a painful API → emits an
    Agent-Skills-compatible `SKILL.md` (first-call guidance) + tool defs. Now the
    comprehended surface is consumable by *any* Agent-Skills/chub-aware runtime, not just
    Gecko's MCP. Gecko fills exactly the gap chub structurally cannot (no hand-authored
    doc exists for the painful long-tail). **LOCAL/BYOD only — never chub's public
    registry** (no-public-catalog holds).
  - **Gecko ← ecosystem (CONSUME):** where a chub doc exists for the same library, Gecko
    can pull it as complementary "what to know" context beside its callable tools — the
    same posture as consuming pay.sh's catalog as an input. Evidence-gated (build only if
    it measurably helps).
  - **COEXIST:** both MCP servers on one agent; `search_capabilities` (Gecko) and `search`
    (chub) are complementary. The cross-surface aggregator stays evidence-gated V2.

## What changes from the four "adopt" specs under COMPOSE

| Item (from the adopt specs) | Reframed under COMPOSE |
|---|---|
| **Agent-Skills `SKILL.md` emit** | **Elevated to THE composition interface** (was "additive emit"). This is thread 2's spine — Gecko emits the interop format so the ecosystem consumes our painful-API surfaces. Extends `agentnative.py` (already emits llms.txt/gecko.json/tools.md), single-sourced from the one `AgentApiClient`. |
| **`SurfaceNote`** | Stays OUR data (thread 1 — the corpus/memory), but is authored **in the Agent-Skills-compatible shape** so the same note that feeds memory/corpus/retrieval also emits as chub-format content. One authored string, now *four* consumers (memory, corpus, BM25 blurb, emitted SKILL). The compose unit. |
| **BM25 / retrieval ranker** | **Do NOT reimplement chub's BM25.** We already have lexical + a gated dense/RRF hybrid. Compose = coexist; keep our OWN tokenizer fix (thread 1) but don't absorb chub's ranker. |
| **`get_capability` verb** | Thread 1 — our own tool improvement (completes ref→resolve→call). Keep. |
| **`annotate`/`update`/`cache`/`build` verbs; public registry; free-text telemetry** | **Reject** (unchanged) — control-plane + no-public-catalog. |

## Everything that ties to the tool (the full compose picture)

```
  comprehension pipeline (ingest → catalog → tools → caller)     [OUR core]
        │ emits
        ▼
  agent-native emit  ── Agent-Skills SKILL.md + llms.txt/gecko.json ──▶  context-hub / any Agent-Skills runtime   [COMPOSE ↔]
        │ serves                                                          (Gecko produces the painful-API docs chub can't)
        ▼
  served MCP  ── coexists with chub's MCP on one agent ──────────────▶  the agent   [COMPOSE ↔]
        │ calls (auth injected)
        ▼
  credential resolver  ── composes OS keychain / op·vault·pass / Keycard-style identity ──   [COMPOSE — the auth seam we just built]
        │
        ▼
  local runner  ── captures wire STATUS only (payload-free) ──▶  correctness corpus (SurfaceNote flywheel)   [OUR moat]
```
Also composed, unchanged: **x402 / pay.sh** (payment rail + catalog as *inputs* — consume,
never re-list). The credential resolver already composes the keychain/secret-manager
ecosystem; **Keycard** (a16z-backed agent identity) is the standard to compose next as the
auth layer matures.

## Build implementation plan (sequenced, Pattern B — falsifier first)

**Phase 0 — our-own improvements, ungated (ships first):**
- `get_capability` verb (`AgentApiClient.get_tool()` + thin `mcp_server.py` wrapper).
- camelCase operationId tokenizer fix in `catalog.py` (real recall bug; only adds recall).
- The offline eval harness (4 retrieval arms × golden sets; hosts the note→FCC eval).
  *These make Gecko better and gate everything after; no chub dependency.*

**Phase 1 — the composition interface (thread 2 spine):**
- Extend `agentnative.py` to emit an **Agent-Skills-compatible `SKILL.md`** (first-call
  guidance) + tool defs, single-sourced from `AgentApiClient`, routed through `_safe`.
- Author the `SurfaceNote` in that same shape → the note becomes the interop artifact.
- Falsifier: a comprehended painful API emits a valid Agent-Skills bundle a chub-aware
  runtime can load — offline, no public publish. LOCAL/BYOD only.

**Phase 2 — the corpus/memory flywheel, gated on measured FCC (thread 1 moat):**
- `SurfaceNote` loop: off-by-default re-injection, untrusted-labeled, validated-at-write;
  `~/.gecko/corpus/` closed-vocab rows keyed by `surface_rev`; `observed` (local runner,
  status-only) + `reported` capture. A note lands only where the eval shows it fixes a call.

**Phase 3 — retrieval, gated on scale (>50 usable ops):**
- BM25 arm (OpenAPI-remapped weights) → dense+RRF only if recall@3 still <0.8. Currently
  18/26 ops → gate un-met; don't touch the live ranker yet.

**Phase 4 — compose-CONSUME, evidence-gated (optional):**
- Gecko pulls a chub doc as complementary context beside its tools — build only if it
  measurably lifts first-call-correctness on a library chub covers.

**Never:** the rejected verbs, a public catalog, free-text/query-text telemetry.

## Decisions / escalations

1. **Confirm the COMPOSE reframe** — Agent-Skills emit is the interop spine; we do NOT
   reimplement chub's ranker/annotation-store; local/BYOD only. *(Founder-level — this doc
   assumes yes.)*
2. **⚠️ Staff-engineer:** V2 corpus — **observed-first** (local runner on-path,
   recommended) vs **reported-co-equal**. The load-bearing architecture call under the
   flywheel; unchanged by the reframe but still open.
3. **Phase 4 (compose-consume)** — build only on measured lift; default is don't.

## Why this is the right shape

It keeps Gecko doing its one differentiated verb ("APIs get USED") and refuses to become a
docs-registry (chub's job) or a public catalog (nobody's job we want). The Agent-Skills
format turns our painful-API comprehension into something the whole agent ecosystem can
consume — **compounding our reach without rebuilding anyone's layer** — while the corpus
flywheel (fed by the local runner) stays our own moat. Compose outward, improve inward.
