# Context-Hub adoption — implementation mapping & build plan

Date: 2026-07-09
Author: software-engineer
Status: SPEC / DESIGN only. No code changes proposed here — this is the concrete
Python mapping and sequencing that ties together the parallel context-engineer,
data-engineer, and ai-ml-engineer specs.

## Scope

Andrew Ng's `context-hub` (chub) was evaluated as a **COMPLEMENT**, not a
competitor, and it's a Node/TS project. We do **not** port it. This doc decides,
verb by verb and artifact by artifact, **what pattern from chub is worth mapping
onto Gecko's existing Python modules**, which module owns each piece (honoring the
thin-transport rule — logic in `gecko/`, never in `mcp_server.py` or a script),
and in what order it ships (Pattern B: first deliverable = a free offline
falsifier).

### The one structural difference that drives every decision

chub is a **curation registry**: humans hand-write `DOC.md`/`SKILL.md` files,
`chub build` indexes them into `registry.json`, a CDN serves them, and the CLI
fetches on demand. Its content is **authored**.

Gecko is a **comprehension pipeline**: we ingest an OpenAPI spec and *generate*
the surface (`ingest → catalog → tools → agentnative`). Our content is
**derived**, per-client, in-memory, and control-plane only.

So the honest framing: **chub's verbs are a superset UI over a static catalog;
most of them are redundant with, or subordinate to, our generation pipeline.**
The genuinely additive ideas are three, and they are narrow:

1. the **doc/skill split** ("what to know" vs "how to do it") as an *emission*
   distinction in `agentnative.py`;
2. **Agent-Skills-spec YAML frontmatter** as the on-the-wire format for anything
   we emit that an agent installs;
3. **`get` as an explicit progressive-disclosure fetch** distinct from `search`.

Everything else is either already covered or should be rejected.

---

## 1. Verb mapping

chub's MCP surface is `search / get / annotate / feedback / update / cache /
build` (`cli/src/mcp/tools.js`). For each: does Gecko HAVE it, should we ADD it
(and where), or REJECT it?

| chub verb | Gecko status | Where it lives / would live | One-line why |
|---|---|---|---|
| `search` | **HAVE** | `mcp_server.py:_SEARCH_TOOL` → `client.search_capabilities`/`AgentApiClient.search` over `catalog.py` | `search_capabilities` already is intent→ranked tools, and it goes further than chub's fuzzy match (lexical + optional dense RRF fusion, auth-filtered, scale-aware). No change. |
| `get` | **PARTIAL → ADD (thin)** | new `AgentApiClient.get_tool(name)` in `client.py`; MCP verb is a thin wrapper | Today `search_capabilities` *inlines* the `inputSchema` for every ranked hit. chub's insight is a separate, explicit **fetch-one-in-full** step (progressive disclosure). We already have the above-scale `to_lightweight_ref` projection that *withholds* the schema — the missing piece is a first-class `get_tool` the agent calls after picking a ref, instead of re-running search. Additive, not redundant. |
| `annotate` | **REJECT (for emitted surfaces)** | — | chub annotations are local user notes appended to fetched docs. chub itself flags them as an untrusted-input / persistent prompt-injection vector (only re-injected on explicit `withAnnotations=true`). For Gecko this collides with invariant #1 (control plane only — we don't persist user-authored payloads against a surface) and adds an injection channel into agent-facing docs we've spent `agentnative._safe` hardening. If a corpus-side "operator note" is ever wanted, that's a data-engineer call on the corpus store, **not** an MCP verb. |
| `feedback` | **HAVE (different shape) — keep ours** | `client._capture` → `corpus.outcome_from` + `emit_surf_event("surf.first_call_correct", …)` | chub's `feedback` is an explicit agent-submitted rating (`rating/comment/labels`). Gecko already captures a **stronger, passive** signal: the actual first-call outcome (ok-bool + error class + latency + synthetic/observed source), control-plane-safe, on every `call()`. That is the corpus-flywheel input. An explicit thumbs-up verb is weaker and optional; if wanted it's a data-engineer decision on `corpus.py`, not a new comprehension path. Do not add a second, self-reported feedback channel that competes with the measured one. |
| `update` | **REJECT** | — | chub `update` refreshes a cached remote `registry.json` from a CDN. Gecko has **no remote catalog to refresh** — the surface is (re)derived from the spec at client construction, and `surface_rev`/`tools_rev` already pin/verify it. Re-ingest *is* our "update". Adding an `update` verb would imply a hosted public catalog, which is a hard non-goal (no public catalog / control-plane discipline). |
| `cache` | **REJECT** | — | chub caches fetched doc files under `~/.chub`. We deliberately don't cache **payloads/content** (invariant #1); the only thing we could cache is a comprehended surface, and that's a devops/perf concern (memoize ingest), not an agent-facing MCP verb. Keep it out of the surface. |
| `build` | **HAVE (as generation) — do not expose as a verb** | `agentnative.build_artifacts` + `comprehend_service.comprehend_submission` (already reachable via the `comprehend_api` meta-tool) | chub `build` turns an authored content dir into a `registry.json` + content tree. Gecko's equivalent is *automatic*: comprehend a spec → `build_artifacts` emits `llms.txt`/`gecko.json`/`.well-known/gecko.json`/`tools.md`. We already have the agent-facing door (`MetaComprehendSurface.comprehend_api`). No new verb; at most, extend what `build_artifacts` emits (see §2). |

### Net additive surface

Exactly **one** new agent-facing verb is justified: **`get`** (fetch one tool's
full contract / doc in isolation), backed by a new pure method on
`AgentApiClient`. Everything else is HAVE or REJECT. This keeps the MCP surface
minimal and keeps logic in the package.

#### `get` — the concrete contract

- **Package (the product):** `AgentApiClient.get_tool(name: str) -> dict` in
  `client.py`. Returns the full callable def for one usable tool — the same
  `{name, description, inputSchema}` plus `_invoke` **already** produced by
  `to_tool`, resolved by name against `self._tool_by_name`, auth-filtered against
  `_usable_tool_names`, and raising a typed `ToolNotFound`/`CallError` (never bare
  `KeyError`) for an unknown or auth-gated-unavailable name. It is a pure lookup —
  no new comprehension, no network, control-plane safe by construction (auth
  headers are already hidden from tool defs, invariant #4).
- **Transport (thin):** a `get_capability` MCP tool in `mcp_server.py` that does
  nothing but call `client.get_tool(...)` and shape the result. ~10 lines, no
  logic.
- **Why it pays off now:** it completes the above-scale story. `list_tools`
  already emits lightweight refs that *tell the agent to fetch the real schema*;
  today the only way to get that schema is to re-run `search_capabilities`.
  `get_capability(name)` is the honest "I already know which tool, give me its
  full contract" step. It removes a search round-trip and makes the
  ref→resolve→call loop explicit.
- **Progressive disclosure, no `--full` needed:** chub's `--full` fetches
  companion reference files. We have no companion files — a tool's full contract
  *is* its schema — so `get` returns the whole thing in one shot. If/when
  `agentnative` emits a per-tool DOC.md (see §2), `get` can grow a `format="md"`
  projection, but that's V2, not the first deliverable.

---

## 2. Content format — Agent-Skills YAML frontmatter

**Decision: adopt the Agent-Skills-spec YAML-frontmatter format as an ADDITIVE
emission in `agentnative.py`, gated behind the existing artifact builder — but do
NOT restructure our storage or make it a second source of truth.**

### What chub's format is

- `DOC.md` ("what to know") and `SKILL.md` ("how to do it"), each a directory with
  a ≤~500-line entry point + companion reference files, YAML frontmatter
  (`name`, `description`, `metadata.{languages,versions,source,tags,updated-on}`),
  interoperable with Claude Code / Cursor / Codex / 30+ agents.

### How it composes with what we already emit — single source of truth

`agentnative.build_artifacts(client)` already derives **everything** from the one
`AgentApiClient` (the single source of truth) and emits four artifacts through the
hardened `_safe` sanitizer. The rule for adopting the chub format is: **it must be
one more projection of that same `client`, never a parallel hand-authored store.**

Concretely:

- **ADOPT** the doc/skill *distinction* as an emission axis:
  - a Gecko surface is a **DOC** ("what this API can do") — this is exactly what
    `tools.md` / `llms.txt` already are. Reframe `tools.md` emission (or add a
    sibling `SURFACE.md`) to carry Agent-Skills frontmatter (`name`,
    `description`, `metadata.tags` from spec tags, `metadata.source`,
    `updated-on` from `surface_rev`) so the artifact is *installable* into an
    agent's context dir natively, the same way a chub DOC.md is.
  - a **SKILL** ("how to call this API correctly first try") is the genuinely
    Gecko-native artifact chub can't generate: our first-call-correct guidance +
    the auth-is-injected-at-call-time behavioral note. If we emit one, it's a new
    `_skill_md(client, meta)` builder alongside `_tools_md`, frontmatter-tagged,
    same `_safe` routing, added to the `build_artifacts` return dict and
    `ARTIFACT_PATHS`. **This is the highest-value format adoption** because it's
    behavioral guidance no OpenAPI dump contains.
- **DO NOT ADOPT**:
  - `languages[]`/`versions[]` nesting — chub needs it because a human authors a
    Python vs JS variant. Our surface is generated from one spec; the "version" is
    already `surface_rev`. Adding language nesting would be schema for schema's
    sake.
  - a separate `registry.json` schema — `gecko.json` **is** our manifest and is
    already a superset for our purpose (it carries `surface`, `operations`,
    `tools`, `surface_rev`, artifact links). If cross-tool interop with the chub
    registry is ever wanted, that's a *rendering* of `gecko.json`, decided with
    data-engineer, not a second stored index.
  - chub's `source: official|maintainer|community` trust tiers — our trust signal
    is `anchor.state` (verified / quarantined) + the poison flag, which is
    stronger and already wired. Don't bolt on a parallel taxonomy.

### The invariant guardrail

Any frontmatter/`.md` we emit is **derived at emit time from `client`, passed
through `agentnative._safe`** (anti-poison → strip markdown structure → redact
secret-shaped tokens → cap). We never *store* an authored doc and never accept one
back (that's the `annotate` rejection in §1). This keeps: one source of truth (the
comprehended client), control-plane only (invariant #1), auth invisible (#4).

---

## 3. Phased build plan (Pattern B)

Each phase's **first deliverable is a free offline falsifier** — a test or the
`gecko.demo` recorded path that can prove the feature wrong with no network, no
subscription, no CDN. Live/interop smoke is always the final check.

This plan sequences **only what the parallel specs conclude is worth adopting.**
The context-engineer spec owns the retrieval/progressive-disclosure rationale, the
ai-ml-engineer spec owns whether a SKILL.md improves first-call-correctness, and
the data-engineer spec owns any corpus-side feedback shape. Below is the Python
sequencing that ties their conclusions together. **Nothing here ships until the
owning spec greenlights its item** (marked ⟶owner).

### Phase 0 — `get_capability` (the one clearly-additive verb) — SMALL

⟶ owner: context-engineer (progressive-disclosure conclusion).

1. **Falsifier first:** unit test in `tests/` — construct an `AgentApiClient` from
   a fixture spec, assert `get_tool("known_tool")` returns the full def with
   `inputSchema` and `_invoke`, raises typed error for an unknown name, and is
   auth-filtered (an auth-gated tool with a no-auth session raises, not leaks).
   A second test: above-scale, `list_tools` ref → `get_tool(ref["name"])`
   recovers the schema `to_lightweight_ref` withheld. Fully offline.
2. `AgentApiClient.get_tool` in `client.py` (pure lookup, typed error).
3. Thin `get_capability` MCP tool in `mcp_server.py`.
4. Recorded smoke: `uv run python -m gecko.demo` still $0-green; add a line
   exercising ref → get → call.

Cost: ~1 method + 1 thin verb + 2 tests. No cross-module contract change.

### Phase 1 — SKILL.md emission (behavioral first-call guidance) — SMALL/MED

⟶ owner: ai-ml-engineer (does a SKILL.md measurably lift first-call-correctness?
If not, **drop this phase** — do not ship format for format's sake).

1. **Falsifier first:** a golden-file test asserting `build_artifacts(client)`
   now includes a `SKILL.md` key with valid Agent-Skills frontmatter, and that
   every emitted field went through `_safe` (feed a poisoned-description fixture,
   assert no `#`/backtick/link syntax and no secret-shaped token survives).
2. `_skill_md(client, meta)` in `agentnative.py`; add key to `build_artifacts`
   and path to `ARTIFACT_PATHS`.
3. Reframe `tools.md` (or add `SURFACE.md`) with the DOC-side frontmatter so both
   halves of the doc/skill split are installable.

Guardrail: keep `agentnative.py` under its ~300-line limit — if `_skill_md`
pushes it over, split the `.md` builders into `agentnative_md.py` (mechanical,
no contract change).

### Phase 2 — (conditional) explicit `feedback` verb — DEFER

⟶ owner: data-engineer. **Default = do not build.** Our passive
`surf.first_call_correct` capture is the corpus input and is stronger than a
self-reported rating. Only revisit if discovery shows agents want to volunteer a
correction signal the passive path can't infer. If built, it's a `corpus.py`
concern with the same control-plane boundary (`outcome_from` — metadata only),
plus a thin MCP verb; never a new comprehension path.

### Explicitly NOT on the roadmap

`update`, `cache`, `annotate`, a `registry.json` schema, language/version
nesting, chub's `source` trust tiers, a public/CDN-served catalog. Each is either
redundant with the generation pipeline or collides with the no-public-catalog /
control-plane invariants (§1).

---

## 4. Staff-engineer flags (cross-module / contract changes)

- **None of Phase 0–1 crosses a module contract** — `get_tool` is a new pure
  method (additive), `_skill_md` is a new emitter behind the existing
  `build_artifacts` return dict. Safe for software-engineer to own directly once
  greenlit.
- **Escalate if** any of these creep in: (a) making the emitted `.md`/frontmatter
  a *stored, re-ingested* artifact (touches invariant #1 and the ingest→emit data
  flow — staff-engineer + data-engineer); (b) adding `annotate`/`feedback` write
  paths that persist agent-authored text against a surface (control-plane
  boundary — staff-engineer + data-engineer); (c) anything that implies a remote
  Gecko catalog/registry (`update`/`cache`) — that's a positioning change (no
  public catalog), not an implementation task.
- The `search_capabilities` return shape is a **frozen agent-facing contract**
  (`{name, summary, path, method}` + inlined `inputSchema`). `get_capability`
  must be *additive* and must not alter that shape. If a future change wants to
  *thin* `search_capabilities` (stop inlining schema now that `get` exists), that
  is a contract change → staff-engineer.

---

## Summary (for the founder)

context-hub is a human-curated doc/skill **registry**; Gecko is an automated API
**comprehension pipeline**. Mapped verb by verb, most of chub's surface
(`update`, `cache`, `annotate`, `build`) is either redundant with our generation
path or collides with our no-public-catalog / control-plane invariants. We already
have a stronger `search` (`search_capabilities`) and a stronger, *passive*
feedback signal (the first-call-correct corpus capture) than chub's self-reported
rating. The genuinely additive ideas are narrow: an explicit **`get`** step
(progressive disclosure — fetch one tool's full contract without re-searching),
and emitting our surface in **Agent-Skills YAML-frontmatter** format so it installs
natively into any agent — most valuably a **SKILL.md** of first-call-correct
behavioral guidance, which no OpenAPI dump contains. Both stay pure projections of
the one comprehended `AgentApiClient` (single source of truth), routed through the
existing `_safe` sanitizer, so no invariant moves. Everything ships Pattern-B:
offline falsifier first, then the thin transport verb.

### Top 3 decisions

1. **Add exactly one verb — `get_capability`** — backed by a pure
   `AgentApiClient.get_tool()` in `client.py`, thin wrapper in `mcp_server.py`.
   It completes the above-scale ref→resolve→call loop. (Phase 0, small, no
   contract change.)
2. **Adopt Agent-Skills frontmatter as an ADDITIVE `agentnative.py` emission,
   led by a `SKILL.md` of first-call-correct guidance — gated on the
   ai-ml-engineer proving it lifts correctness.** Do not adopt chub's
   `registry.json`, language/version nesting, or `source` trust tiers; `gecko.json`
   + `anchor.state` already cover those, stronger.
3. **Reject `annotate`, `update`, `cache` outright.** They imply persisted
   agent-authored content and a remote catalog — both violate the control-plane /
   no-public-catalog invariants. Any corpus-side feedback stays the existing
   passive `surf.first_call_correct` capture (data-engineer owns any change),
   never a competing self-reported channel.

### References (parallel specs — do not duplicate)

- context-engineer spec — retrieval / progressive-disclosure rationale behind `get`.
- ai-ml-engineer spec — whether a SKILL.md measurably improves first-call-correctness (gates Phase 1).
- data-engineer spec — corpus storage / any feedback-shape decision (gates Phase 2).
