# Gecko architecture

> The three views, with interactive versions and the agent-readable summary:
> [full pipeline (interactive)](assets/architecture.html) ·
> [context engineering (interactive)](assets/architecture-context.html) ·
> [the on-chain loop (interactive)](assets/architecture-onchain.html) ·
> [architecture.llms.txt](../architecture.llms.txt)

# Gecko — the architecture, in three views

> The knowledge graph for APIs your agent can trust. Open-source, runs on your machine.
> One command maps any API — even the messy, paywalled, or on-chain ones — into a
> verified graph your agent **traverses instead of guessing**, and every action can be
> simulated to a **receipt** before money moves. This page: how it's built, what's
> proven, and what isn't yet.

---

## View 1 — Context engineering (the memory substrate)

![Gecko context engineering — semantic, procedural, episodic, working memory](assets/architecture-context.png)


Gecko is **not the agent** — it is the memory-and-context substrate *under* other
people's agents. Every block of the canonical agent-memory architecture exists here,
and the three that differ from the textbook are the product.

```mermaid
flowchart LR
    subgraph EXT["THE AGENT (not us — theirs)"]
        LLM["LLM + Orchestrator<br/>Claude Code · Bankr · any MCP host"]
    end

    subgraph GECKO["GECKO — the substrate"]
        subgraph SEM["Semantic memory — no vectors, on purpose"]
            KB["Comprehended surfaces<br/>ingest.py · pda_extract.py · orquestra_comprehend.py"]
            CAT["Lexical catalog (token-overlap)<br/>catalog.py — deterministic; BM25 + vectors evidence-gated"]
        end
        subgraph PROC["Procedural memory — plans as typed data"]
            TOOLS["Question-shaped tool defs<br/>tools.py — auth stripped"]
            PLANS["landing_plan · derivation_order<br/>executable JSON, not prose"]
        end
        subgraph EPI["Episodic memory — categorical, self-generated"]
            CORPUS["corpus.py — closed vocabularies<br/>observed / reported / synthetic / simulated"]
            DRIFT["drift.py — N-confirmed drift series<br/>'clean at slot S, reverts at S′'"]
        end
        WM["Working-memory projection<br/>just-in-time slices · −77/−89% tokens"]
    end

    KB --> CAT --> WM
    TOOLS --> WM
    PLANS --> WM
    WM -->|"MCP / CLI"| LLM
    LLM -->|"intent"| WM
    CORPUS --> DRIFT
    DRIFT -->|"what changed"| KB
```

**The three deliberate differences:** semantic memory that never *approximately*
remembers (lexical, evidence-gated against vectors) · episodic memory that stores
**categories, never payloads** — and generates its own episodes by re-simulating ·
procedural memory as **typed plans** a builder can execute, not text an LLM re-reads.

---

## View 2 — The full architecture (sources → knowledge → action)

![Gecko full architecture — untrusted sources → provenance knowledge → verified action](assets/architecture.png)


```mermaid
flowchart TB
    subgraph SRC["SOURCES — all untrusted"]
        OAS["OpenAPI / docs / llms.txt"]
        IDL["Anchor IDL / program source (Steel too)"]
        OC["Orquestra catalog — 4,500 projects"]
    end

    subgraph COMP["COMPREHENSION"]
        SAN["sanitize / quarantine / Skill Guard<br/>anti-poisoning at ingest"]
        ING["ingest → Operation/Param"]
        PDA["pda_extract → seed recipes<br/>+ merge: source rescues what the IDL drops"]
        AUTO["auto-comprehend on pick<br/>generated config + overlay (measured)"]
    end

    subgraph KNOW["KNOWLEDGE — every edge carries provenance"]
        SG["Surface graph<br/>EXTRACTED &gt; DECLARED &gt; INFERRED &gt; CLAIMED→VERIFIED/REFUTED"]
        PG["Program graph<br/>EXTRACTED / RECOVERED / FLAGGED"]
        CORR["Correlation engine<br/>cross-API joins, DECLARED-first"]
    end

    subgraph PROJ["PROJECTION"]
        MCP["MCP surfaces (hosted · stdio · npx/uvx)"]
        CLI2["CLI · Scorecard · Playground"]
    end

    subgraph ACT["ACTION — verify, never execute"]
        PLAN["plan_* → full account set<br/>+ state-read args + landing preludes"]
        BUILD["Orquestra /build (THEIRS)"]
        SIM["simulate → RECEIPT<br/>$0 · surfpool/RPC · never signs"]
        GATE["signing gate (fail-closed, verdict-based;<br/>message-hash bind planned)"]
        SIGN["signer: wallet / 1claw / founder (THEIRS)"]
    end

    SRC --> SAN --> ING & PDA & AUTO
    ING --> SG
    PDA --> PG
    AUTO --> PG
    SG & PG --> CORR
    SG & PG --> PROJ
    PROJ -->|"agent intent"| PLAN
    PLAN --> BUILD --> SIM
    SIM -->|"pass (verdict)"| GATE --> SIGN
    SIM -->|"categorical outcome"| KNOW2["corpus + drift series"]
    KNOW2 -.->|"what drifted"| KNOW
```

**The hard boundaries:** Gecko never signs, never broadcasts, never proxies the data
plane (control-plane invariant: surfaces + correctness metadata, never payloads).
Building and signing belong to compose partners — that is the design, not a gap.

---

## View 3 — The on-chain action path (the proven loop)

![The proven on-chain loop — intent → derive → build → simulate → receipt → sign](assets/architecture-onchain.png)


```mermaid
sequenceDiagram
    participant A as Agent
    participant G as Gecko
    participant O as Orquestra (builds)
    participant S as surfpool / RPC
    participant W as Signer (external)

    A->>G: "buy this token on pump"
    G->>G: derive the full account set<br/>(incl. IDL-hidden: bonding_curve_v2,<br/>recovered: creator_vault, base_factor)
    G->>S: control-plane reads (curve reserves → max_sol_cost)
    G->>O: /build (declared plan)
    O-->>G: built instruction
    G->>G: assemble preludes UNSIGNED<br/>(ATA idempotent + compute budget)
    G->>S: simulateTransaction (sigVerify:false)
    S-->>G: ✅ RECEIPT: pass, 86,669 CU<br/>(naive path: ❌ 3012 revert)
    G-->>A: receipt + landing_plan
    A->>W: sign (only a passing, bound receipt)
    Note over G: outcome → corpus (categorical, opt-in `record_to`) → `gecko drift` series
```

Live, verbatim: **pump buy** — naive ❌ `account_error (3012)` → Gecko bundle ✅
`pass, 86,669 CU`. **Meteora swap** — wrap → swap across live bins → unwrap, ✅
`81,964 CU`. Both at $0, before any spend.

---

## What's WORKING (proven) vs NOT BUILT yet

| Layer | ✅ Working & proven | 🚧 Not built yet |
|---|---|---|
| **Comprehension** | OpenAPI + docs ingest · anti-poison quarantine · Skill Guard (image/encoded) · PDA recovery (Anchor + Steel source) · auto-comprehend w/ measured overlays (4 programs, differential-proven) | catalog breadth (4,500 projects listed, 4 wired) · non-Anchor beyond ORE |
| **Knowledge** | surface graph + VERIFIED/REFUTED · program graph + FLAGGED honesty · cross-API correlation (DECLARED-first) · one unified provenance module (`gecko/provenance.py`) | semantic tier (evidence-gated OFF) |
| **Projection** | hosted + local MCP · question-shaped tools, auth invisible · scale-adaptive listing (defs on demand) · −77%/−89% context cuts (two real specs) · Scorecard/Playground · `find_start` intent router | public-docs refresh |
| **Action/verify** | simulate→Receipt (pump + Meteora live proofs) · landing preludes · state-read args · never-sign AST boundary | pump `sell` round-trip · ORE claim / MetaDAO fund intents · hosted point-&-simulate |
| **Memory** | categorical corpus (4 tiers) · N-confirmed drift detector · guarded recipe_hash · opt-in `record_to` wiring on the landing orchestrators + `gecko drift` reader | drift **scheduler** (cadence) · cross-customer pooling (consent-gated) |
| **Security** | SSRF netguard · auth firewall (out-of-band anchor) · keyring/pointer secrets · signing gate v1 | `evaluate_tx` message-hash binding · 1claw TEE credential backend · x402 live billing |

*Honest one-liner: the engine and the proofs are real; the breadth (catalog scale), the
cadence (drift scheduler), and the payer (WTP) are the open frontier.*
