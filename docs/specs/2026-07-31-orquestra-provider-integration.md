# The Provider Integration — Gecko × Orquestra (agent front door → their builder)

**Status:** milestone spec (2026-07-31). Builds on the Program Surface
(`docs/specs/2026-07-30-program-surface-pda-graph.md`, shipped 0.9.x) and the settled
integration model (`private/use-cases/orquestra-integration-model.md`). Compose partner:
Orquestra (Berkay).

## The one-line

> A **repeatable, per-provider** engine that turns any Orquestra-indexed program into a
> **Gecko agent-front-door surface**: intent-shaped tools that translate a plain-English
> intention into the right instruction, complete the derivation (the root seeds the IDL
> drops), and **point the agent at Orquestra's builder** to execute.

Parameterized by program under `providers/orquestra/<program>`. **Not** a hand-built mount
per program; **not** a proxy — we are the metadata/control-plane front door, Orquestra is
execution. ORE stays a separate no-IDL capability proof, not the template.

## The model (settled)

```
plain intention → GECKO surface (intent-shaped tools + complete derivation + "call HERE")
                → ORQUESTRA /instructions/:name/build (execution)
```

The agent connects to **us** first, we translate intention → the correct call + derive the
PDAs, and it executes on **his** API. Zero new infra for him (no callback/gateway). We
never sit in the data path and never sign.

## What each piece owns

- **Orquestra:** the indexed programs (instructions/accounts) + the tx builder. Breadth +
  execution.
- **Gecko:** intent-shaped comprehension (intention → the right call — *the thesis*) +
  derivation completeness (recover the helper-seeded roots the IDL drops) + routing the
  build to Orquestra's endpoint. Discovery + first-call-correctness.
- **The agent:** the high-level multi-hop plan ("buy water" → pay → swap). Not us.

## Config shape (`providers/orquestra/`)

```
providers/orquestra/
  provider.json          # base_url, build endpoint template: /api/{project}/instructions/{name}/build
  meteora/
    program.json         # { orquestra_project, program_id, source: <repo@commit path(s)> }
```

A `ProviderConfig` + per-program `ProgramConfig` — data, not code. The engine consumes them;
adding a program is a config entry + a source pointer, never a new module.

## The assembly pipeline (the engine)

1. **Comprehend the program surface** — pull Orquestra's `/instructions` + `/pda` for the
   program → question-shaped tool defs (Gecko's comprehension; intent-shaped, not raw
   instruction names).
2. **Recover the dropped roots** — `from_source` on the program source → the PdaNodes the
   IDL loses (the `min/max` roots; shipped 0.9.1). Genuinely-unresolvable → honest resolver.
3. **Merge** — Orquestra's breadth + Gecko's recovered depth → a complete derivation graph
   (`merge_pda_nodes` + the instruction↔PDA join, already built).
4. **Route execution** — each tool carries the pointer to Orquestra's `/build` (with the
   derived accounts to send). The agent's plan = derive here → call his builder there.
5. **Intent-shape** — project the tools to intention-level ("swap tokens", "add liquidity"),
   so the agent maps intent → tool → derive → build.

## Output

A servable **agent front-door surface** (the existing `ProgramGraphSurface` shape, extended
with build-routing): `get_program_graph`, `derive_pda`, and a new `plan_intent` /
`plan_instruction` that returns *derive-then-call-his-builder* as one plan. Keyless, control
plane only.

## Phases (each: TDD, gate, PR)

- **Phase 0 — the engine + config.** `ProviderConfig`/`ProgramConfig` loaders; the assembly
  pipeline (comprehend Orquestra surface + merge source-recovered seeds + build-routing
  metadata). Falsifiable offline with a recorded Orquestra `/instructions` fixture.
- **Phase 1 — the Meteora instance (`providers/orquestra/meteora`) — the Berkay demo.** Wire
  Meteora's Orquestra project + program id + source; produce the surface; `derive_pda(lb_pair,
  {WSOL, USDC, bin_step=4})` → `5rCf1DM8…`. **This is the thing to show him.**
- **Phase 2 — intent-shaping + `plan_intent`.** "swap SOL for USDC" → the swap instruction +
  the derive plan + the build call. The intention→code translation, first-class.
- **Phase 3 — serve it (the front door).** Serve the Meteora surface so an agent
  `claude mcp add`s it and runs the flow. (Hosting granularity = per provider, resolved with
  Berkay — a Gecko-hosted demo endpoint, or he serves it.)
- **Phase 4 — the MVP proof.** "swap 1 SOL → USDC on Meteora" → pick swap → derive `lb_pair`
  → call Orquestra `/build` → the tx **simulates clean on a surfpool mainnet fork** ($0).

## Invariants

Control plane only (metadata; never response payloads/keys, never sign). We **point at** his
builder, never proxy it. Per-provider, parameterized by program — config, not code.
Intent→code is ours; multi-hop commerce is the agent's — don't over-promise. Honest resolver
for genuinely-unresolvable seeds. Pattern B: recorded/offline first, surfpool live-sim last.

## Lanes

`staff-engineer` — the engine contract + the build-routing seam (crosses comprehend/derive).
`ai-ml-engineer` — intent-shaping quality (intention → the right tool). `web3-engineer` — the
`/build` call + surfpool simulate. `software-engineer` — the config loaders + assembly.
