# The Program Surface — the instruction↔PDA graph for Solana programs

**Status:** milestone spec (2026-07-30). The on-chain twin of the Agent Surface. Pairs with
`docs/trust-boundary.md`, `docs/specs/2026-07-29-arazzo-spdg-orchestration-plan.md`,
`docs/specs/2026-07-19-surface-graph-correlations-design.md`. Compose partner: Orquestra (Berkay).
Design/proof grounded in `private/use-cases/orquestra-analysis-plan.md`.

## The one-line

> Turn any Solana program into the **deterministic instruction ↔ account ↔ PDA ↔ seeds graph** an
> agent (or an orchestrator like Orquestra) traverses to build a correct transaction — by
> **recovering the PDA seed recipes the IDL/llms.txt loses.**

We build the **graph/context, plug-and-play**. The consumer (Berkay's service) orchestrates and
signs. We are not the orchestrator and never sign (planner/context + trust boundary, on-chain).

## Why — the gap, proven

Orquestra converts program IDLs → REST/MCP/llms.txt, but **the PDA seed recipes are absent** for the
meaningful accounts (Meteora DLMM: of 1,628 account slots only ~72 marked PDA, all `event_authority`).
Root cause (Anchor issue #4057, maintainer-confirmed): Anchor's IDL macro *syntactically* matches
`seeds = [...]`; when a seed is a helper-function output (e.g. `max_key(a,b)` — the standard AMM
pool-pair ordering) it **silently omits the whole `pda` block**. So the agent can't derive
`lb_pair`/`reserve_x`/`position` → can't build the tx. Pointing at llms.txt doesn't help — the info
isn't there.

**Proven recoverable + correct (ORE / Steel, no IDL):** recovered `config`/`treasury`/`board`/`miner`
seed recipes from source → derived the PDAs → **matched on-chain ground truth**, and on a **surfpool
mainnet fork** the derived addresses **hold the real deployed ORE accounts** (owner=ORE). Full
`recover → derive → verify-vs-real-state → simulate` chain proven at **$0**, no keys, no broadcast.

## Design (locked, from the research + Context7)

- **Target format = Codama's node model, re-modeled as Python dataclasses** (`gecko/pda.py`):
  `PdaNode{name, seeds[], program_id?}`, `ConstantPdaSeedNode{type, value}`,
  `VariablePdaSeedNode{name, type}`, `PdaSeedValueNode{name, value: Account|Argument}` (the
  per-instruction edge), and **`ResolverValueNode{name, depends_on[]}` — the honest escape hatch**
  for seeds we can't statically resolve (Anchor `max_key`, cross-program, hashed). Never fabricate a
  seed, never silently drop the account (Anchor's own bug). *That honest flag is the differentiator.*
- **Extraction, ranked:** (1) parse Anchor ≥0.30 IDL `pda.seeds` — but a *missing* `pda` on a
  PDA-shaped account is a **flagged gap**, not "not a PDA"; (2) source regex on
  `#[account(seeds=[...])]` (legacy Anchor, + cross-check 0.30+); (3) source regex on
  `*_pda()`/`Pubkey::find_program_address(&[...])`/`has_seeds(&[...])` (Steel/native — machine-regular,
  one regex caught every ORE PDA); (4) LLM only to *propose* a `ResolverValueNode` (deps +
  description), never a concrete value. Flag-not-solve: runtime-data seeds, hashed seeds,
  `remaining_accounts`, cross-program closed-source PDAs, ATA derivations.
- **Derivation:** `find_program_address` via **solders**, behind an optional `[solana]` extra (engine
  stays dep-light; model + extraction are pure stdlib).
- **Testing:** **surfpool** `$0` mainnet-fork `derive → verify-vs-real-state → build → simulate`
  (`simulateTransaction`, never `sendTransaction`); Pattern B — offline ground-truth first, surfpool
  live-sim last. Safe to script (local validator, not mainnet).
- **Output:** a **structured JSON graph** (plug-and-play — Berkay's orchestrator ingests it) + an
  intent-tool projection (the paybox UX: "buy water" → the derive→build plan). **Not a text llms.txt.**

## Invariants (do not violate)

1. **Control plane #1** — model the program *surface* (instructions, accounts, PDA seed recipes).
   Never store on-chain response data, account values, keys, or secrets. `fetch_pda_data`-style
   account data is untrusted input → Skill Guard on ingest; keyless.
2. **Honesty** — un-resolvable seed → `ResolverValueNode` (flagged, with partial deps). Never
   fabricate, never silently drop. This is the claim we make over "read the IDL and hope."
3. **Engine dep-light** — `gecko/pda.py` model + extraction are pure stdlib; derivation (solders) is
   an optional `[solana]` extra.
4. **We are not the orchestrator / never sign** — emit the graph + the prepared plan; the consumer
   orchestrates and the user's wallet signs (founder-gated on-chain, as always).
5. **Pattern B** — $0 offline/recorded first (derive vs ground-truth consts); surfpool live-sim last.

## Phases (each: TDD, gate, one PR)

- **Phase 0 — `gecko/pda.py`: the seed-graph model + derive.** The Codama-shaped frozen dataclasses;
  `derive_pda(node, bindings) -> (address, bump)` via solders (`[solana]` extra). Test: the ORE
  `config`/`treasury`/`board`/`miner` `PdaNode`s → derive → match the proven ground-truth addresses.
- **Phase 1 — extraction.** `from_anchor_idl(idl)` (0.30+ `pda.seeds`, missing→gap) and
  `from_source(rust)` (Steel/legacy regex: `*_pda`/`find_program_address`/`has_seeds`) → `{name:
  PdaNode}`; un-resolvable → `ResolverValueNode`. Tests on the ORE source + a Meteora-style
  missing-pda IDL (→ flagged resolver).
- **Phase 2 — the instruction↔PDA graph.** Join extracted `PdaNode`s to instruction accounts (from
  Orquestra's REST `/instructions` or the IDL) → the derivation DAG (derive order, deps on other
  accounts/args) → structured JSON. The "same graph we built for APIs, extended to programs/PDAs."
- **Phase 3 — plug-and-play output + intent projection.** Emit the graph as structured JSON for an
  orchestrator; project intent-shaped first-call-correct tools (derive→build chain). Optional MCP
  serve for the demo.
- **Phase 4 — the surfpool `$0` test harness.** Fork mainnet → derive from the extracted graph →
  verify vs real state → build → simulate → assert no `ConstraintSeeds`/`InvalidSeeds`. The
  correctness gate per recovered recipe.
- **Phase 5 (later) — enrichment.** Optional Codama-JS `rootNodeFromAnchor` importer for Anchor IDLs;
  Shank (Metaplex) framework; cross-program + ATA seed schemes.

## Lanes

`software-engineer` — the `pda.py` model + derive + the graph assembly (Python/dataclasses).
`ai-ml-engineer` — the extraction quality (source→seed-graph, the resolver-vs-fabricate calls).
`web3-engineer` — the surfpool harness + on-chain simulate + Solana-tx specifics. `defi-security`
if the Skill-Guard-on-account-data path changes. `staff-engineer` for the graph contract if it
crosses `graph.py`/`correlate.py`.
