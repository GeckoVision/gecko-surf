# The Agent Surface — reformat for clarity (not a thesis change)

**Date:** 2026-07-23
**Trigger:** `ARXIV_TRENDS_AND_PRD.md` (founder) + the instruction: *bring clarity, don't
change the thesis; we are building a new surface for agents — a software projected for
them — not a better OpenAPI/MCP.*
**Rule:** this document renames and clarifies what is **already shipped**. It changes no
behavior and no thesis. Every claim below maps to code that exists.

---

## 0. The one sentence

> **Gecko projects the Agent Surface: the deterministic, provenance-carrying, safety-checked
> *call graph* an agent traverses to make the right call the first time — derived
> unilaterally from any spec, and kept correct as that spec drifts.**

OpenAPI and MCP do not go away. The Agent Surface **sits above them and composes on them** —
it is the layer they were never designed to be.

---

## 1. What is actually new (the layer nobody else fills)

The clearest way to see it — three layers, not one:

| Layer | Answers | For whom | Format |
|---|---|---|---|
| **Shape** | *what endpoints/fields exist* | humans + codegen | OpenAPI, GraphQL |
| **Transport** | *how an agent invokes one tool* | agents (still guessing which + args) | MCP |
| **Surface** ← us | ***given intent: which chain of calls, in what order, with what confidence, from what basis — and is it safe*** | agents, first-call-correct + auditable | **the Gecko Surface** |

Shape and transport are **probabilistic at the point of use**: the agent still *guesses*
which endpoint, which sequence, which args, whether the spec is trustworthy. The Surface is
the **deterministic** answer to those guesses. That is the founder's "probabilism →
determinism," made concrete: we do not make the *model* deterministic — we hand it a
deterministic **surface** so the model does not have to guess.

This is why it is "a software projected *for* agents": a human reads prose and infers the
sequence; an agent needs the sequence *materialized* — as a graph with provenance on every
edge. Humans never needed that artifact. Agents do. We build the artifact humans never
needed.

---

## 2. It already exists — it is just unnamed

The Agent Surface is not a thing to invent. It is shipped, scattered across four modules
under four different names. The reformat is to **name it once**:

| The Surface is… | Shipped in | Today's name |
|---|---|---|
| the deterministic call graph + per-edge provenance (`EXTRACTED`/`DECLARED`/`INFERRED`, confidence, basis) | `gecko/graph.py` | `SurfaceGraph`, `Plan` |
| the question-shaped tools (intent → the right op, auth hidden) | `gecko/tools.py` | tool defs |
| the safety verdict (untrusted-spec sanitizer, per-tool quarantine, fail-closed routing) | `gecko/sanitize.py` | anti-poisoning |
| the agent-native projection (`llms.txt` / `gecko.json` / `.well-known`) | `gecko/agentnative.py` | manifest artifacts |
| the wire (MCP Streamable-HTTP) | `gecko/mcp_server.py` | `McpSurface` |

Four names, one object. **"Surface" should be the first-class noun** the codebase and the
pitch both use for it.

---

## 3. Honest read of the trends doc — what's real vs what's not our lane

The ARXIV doc is directionally right on the macro shift and **inflates the product into an
enterprise platform.** Being straight about the split is the point of this section.

**Real, shipped, ours — keep and lead with:**
- **Determinism over probabilism** (Trend 1) → the surface graph + plan. *Shipped.* The
  doc's own example JSON (`edges` with `provenance`/`confidence`/`order`) **is our graph.**
- **Provenance & auditability** (Trend 5) → per-edge `EXTRACTED/DECLARED/INFERRED` +
  confidence + basis + explain. *Shipped.* This is a genuine differentiator over OpenAPI/MCP.
- **Anti-poisoning** (Trend, §3) → `sanitize.py` + quarantine. *Shipped, and comprehension-
  native* (we defend the surface we derive, not a generic firewall).

**Real trend, but a COMPOSE lane — do NOT build:**
- **Human-in-the-loop security** (Trend 3): runtime approval, spend policy, scope prompts.
  That is [[compass-partnership]] / 1claw / Privy territory ([[1claw-orquestra-eval]]). We
  feed them a *verdict*; they hold the human gate. Building approval UIs dilutes the thesis.
- **Multi-agent orchestration / A2A** (Trend 4): not our layer. We make one agent's calls
  correct; agent-to-agent coordination is someone else's protocol.

**Aspirational metrics that are not commitments** — the doc lists SOC2/HIPAA, 10,000
concurrent agents, 100,000 skills, `<200ms` A2A, "50 enterprise customers." These are a
wishlist, not a roadmap. Naming them as targets before consumer WTP is validated repeats
the over-claim failure. Keep them out of the canon.

**The doc's competitive table is right but for one word:** it says the moat is that only
"Gecko Surface" has the deterministic graph + provenance + anti-poisoning. True *today*. But
a **format is copyable** (MCP and OpenAPI are open standards anyone implements). See §4.

---

## 4. The moat — honestly (the format is not it)

The seductive error in the doc: *"we build a new format, therefore we have a moat."* A
format is not a moat — MCP shipped and was cloned in weeks; OpenAPI is universal precisely
because it is free to implement. If the Agent Surface were only a schema, a competitor
copies the schema.

The moat is **not the surface — it is deriving the surface well, from anything, and keeping
it correct.** Three compounding edges, in order of defensibility:

1. **Derivation quality (now).** Anyone can define a call-graph format; almost nobody can
   *infer* it correctly from a raw, messy, drifting spec with no call logs. That is the
   Stripe control (66,984 → 337 false links, −99.5%), the entity-signature trust ladder,
   the genericity demotion. This is hard, it is ours, and it is the wedge — [[three-pillar-thesis]].
2. **Correctness under drift (the real product).** A surface that is right the day you
   derive it and wrong a week later is worthless. `gecko test` + the hosted drift-watch keep
   it first-call-correct as the API changes. This is what a *provider* actually buys, and
   it is the thing OpenAPI/MCP structurally cannot do (they are static documents).
3. **The correctness corpus (the compounding bet, unproven).** Every derived-and-verified
   surface teaches the inference. This is the flywheel — real only once proven on one API
   ([[moat-corpus-flywheel]], [[three-pillar-thesis]]). Do **not** claim it exists yet.

So: **name the surface** (clarity, positioning), but **sell the derivation + correctness**
(moat). The new format is the *shape of the pitch*; the inference engine is the *substance*.

---

## 5. The reformat — two moves, zero thesis change

### 5a. Positioning (copy + docs)
- Retire "make any API agent-usable" as the *lead* — it undersells to "better wrapper."
  Lead with: **"Gecko projects the Agent Surface — the call graph your agent traverses right
  the first time."** ([[positioning-grunt-line]] stays the plain-language subhead.)
- Everywhere we currently say "surface" ambiguously, mean the **one** thing in §2.
- The README competitive frame becomes the §1 three-layer table: *shape / transport /
  surface*, composing not replacing.

### 5b. Code (clarity refactor — behavior-preserving)
The goal is that a new reader meets **one named artifact** instead of assembling it from
four modules. Concretely, propose (in order, each its own PR, all no-op on behavior):
1. A `gecko/surface.py` façade: a `Surface` value object that *composes* the existing
   `SurfaceGraph` + tool defs + sanitizer verdict + agentnative manifest — a single typed
   handle an agent (or the MCP layer) consumes. It **wraps**, it does not rewrite; the
   engine modules are untouched.
2. Rename in docstrings/comments (not APIs) so "the Surface" is the consistent noun; keep
   `SurfaceGraph`/`McpSurface` as its *components*, documented as such.
3. One projection entry point: `Surface.project(kind)` → the existing `llms.txt` /
   `gecko.json` / tool-def / MCP projections (the "one manifest → N projections" already
   sketched in `agentnative.py` and [[agent-native-surface-design]]).

No new wire format. No reinventing MCP/OpenAPI. We *name and unify* what ships.

---

## 6. Guardrails (so "new surface" doesn't drift into the wishlist)

- **Compose, never replace.** OpenAPI is an *input*, MCP is a *transport*. The Surface
  consumes both. We are not a new protocol on the wire.
- **The format is not the moat.** Never pitch the schema as defensibility; pitch derivation
  + correctness (§4).
- **Stay in lane.** Human-approval, custody, multi-agent, A2A = compose partners. A verdict
  from us, the gate from them.
- **No aspirational metrics in the canon.** SOC2/10k-agents/50-customers are not roadmap
  until WTP is validated.
- **Honesty gate.** The corpus flywheel is a bet, not a fact. Say "deriving + keeping
  correct," not "we have a data moat."

---

## 7. Immediate next action

Positioning is a doc-and-README pass (cheap, high-leverage). The code reformat is the
`gecko/surface.py` façade (5b.1) — a behavior-preserving unification that makes the thesis
legible in the code the way this document makes it legible in prose. Neither changes the
engine; both make what we already are impossible to mistake for "a better Swagger."
