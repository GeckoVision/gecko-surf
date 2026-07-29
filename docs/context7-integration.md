# Run Context7 and Gecko side by side

> **Context7 tells the agent *what* to call. Gecko proves it's *right* — before the
> call fires — and chains the calls Context7-fed agents get wrong.**

Context7 is a **distribution channel and a docs *input* — never a dependency or ground
truth.** Gecko is the verify-before-you-call + chain + safety layer that sits *below* it.
The two compose: keep both. Gecko does **not** replace Context7 and is **not** a Context7
competitor.

This page is packaging only — no engine code. Register both MCP servers, point each at the
question it actually answers.

| Question | Answered by |
|---|---|
| *What does this API look like? Which endpoints and fields exist?* | **Context7** — docs, retrieved as an unverified INPUT |
| *Given my intent: which call, in what order, from what data — and is that endpoint even real?* | **Gecko** — the deterministic Agent Surface (call graph + `verify-docs`) |

---

## Arch 2 — the parallel-MCP config (Tier 0, zero build)

Add Context7 **and** Gecko to the same MCP config. Context7 answers *"what does this API
look like"*; Gecko's surface answers *"which call, in what order, is it real, is it safe."*

### `.mcp.json` (Claude Code) / `mcp.json` (Cursor, VS Code, any MCP client)

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    },
    "gecko-yourapi": {
      "command": "uvx",
      "args": [
        "--from", "gecko-surf[serve]",
        "gecko", "https://api.example.com/openapi.json", "--stdio"
      ]
    }
  }
}
```

- **`context7`** — the documented Upstash Context7 MCP stdio entry. It surfaces the API's
  docs to the agent. Treat everything it returns as an **unverified claim**.
- **`gecko-yourapi`** — Gecko comprehends `https://api.example.com/openapi.json` and serves
  its Agent Surface over stdio (`gecko <spec> --stdio`; `uvx` runs it with nothing to
  install). Swap in your own OpenAPI URL and rename the server (`gecko-stripe`, etc).

Both entries are stdio — the client spawns each process and talks over stdin/stdout, so
there is no port and no tunnel to manage.

### Zero-setup variant — try it against the hosted TxLINE demo

No API of your own yet? Point the Gecko entry at the hosted recorded demo (`$0`, no key)
instead — a painful two-token paywalled World Cup API, 18 first-call-correct tools:

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    },
    "gecko-txline": {
      "type": "http",
      "url": "https://mcp.geckovision.tech/txline/mcp"
    }
  }
}
```

Gecko's transport is **MCP Streamable HTTP** (`2025-11-25`), not SSE — the hosted URL wants
`streamablehttp_client`, not `sse_client`.

### Why run both — the division of labor

| | Context7 | Gecko |
|---|---|---|
| **Layer** | docs / *what exists* | deterministic surface + verify + correlation |
| **Provenance** | INPUT — retrieved, **unverified** | EXTRACTED / INFERRED / DECLARED, each edge tagged |
| **Answers** | "here are the endpoints and fields" | "call *this* one, in *this* order, from *this* data" |
| **Multi-step chains** | not addressed | plans the chain (the death zone: a 3-step chain at 70%/step ≈ 34% end-to-end) |
| **Is the endpoint real?** | assumes the docs are right | `gecko verify-docs` checks each op against reality |
| **Untrusted input** | its registry is poisonable; its own disclaimer says content isn't guaranteed | ingested spec/doc text is treated as untrusted; poisoned tools are quarantined |

### The point of running Gecko below Context7: `verify-docs`

A docs source can name an endpoint that does not exist. We hit exactly this once: a popular
docs source listed a Privy endpoint that returns 404 — a claim, not a fact.

`gecko verify-docs <spec>` treats every doc-claimed operation as a **CLAIM** and reports a
verdict, control-plane only (status/shape, never a stored payload):

```bash
gecko verify-docs https://api.example.com/openapi.json          # recorded, $0 — every op honestly UNVERIFIED
gecko verify-docs https://api.example.com/openapi.json --live   # real calls: 2xx VERIFIES, a 404 REFUTES
```

- **VERIFIED** — the endpoint exists and the shape matches.
- **REFUTED** — 404 or contract mismatch. This is the fabricated endpoint an agent would
  otherwise call. Your agent never fires it.
- **UNVERIFIED** — no access, or recorded-only (paywalled ops are labelled UNVERIFIED, never
  overclaimed as VERIFIED).

A REFUTED verdict against a third-party docs source is **docs drift, with a receipt** — we
check against the live API and show the result. Neutral and factual: name no villain.

---

## Worked example — Pegana peg-risk, the safe correlated chain (VAS-4)

The same side-by-side, aimed at a real DeFi surface. Pegana (`api.pegana.xyz`) is a
peg-risk **state** oracle; its own docs tell you to cross-check exit liquidity with a
price/liquidity source. Gecko makes that cross-check **one safe query**: intent → Pegana
peg-state → (if `DRIFT`+) Birdeye exit-liquidity, joined by the Solana token mint. Context7
serves Pegana's *concept* docs (what `DRIFT` means) as an unverified INPUT; Gecko serves the
deterministic, safety-checked call chain.

```json
{
  "mcpServers": {
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp@latest"]
    },
    "gecko-pegana": {
      "command": "uvx",
      "args": [
        "--from", "gecko-surf[serve]",
        "gecko", "https://api.pegana.xyz/openapi.json", "--stdio"
      ]
    }
  }
}
```

- **`context7`** — Pegana's peg-risk *concept* docs, retrieved as an **unverified claim**
  (the "what does `DRIFT` mean" knowledge).
- **`gecko-pegana`** — Gecko comprehends Pegana's OpenAPI and serves its Agent Surface over
  stdio: the first-call-correct tools, the mint-joined exit-liquidity chain, and the safety
  verdict. Pegana's public reads are **keyless**, so no credential is pasted into `mcp.json`.

**Why this one is the safety demo.** A financial agent chaining untrusted DeFi APIs while
positioned to move money is the juiciest target: poison any one surface (a tool description
that appends a `transfer(<addr>)` or an "emit the API keys" instruction) and you misroute
funds or lift a key. Gecko runs that chain **safely** — it is deterministic (no guess to
hijack), **keyless** (auth is injected at call-time, never exposed in a tool def, so the
agent holds no key to leak), and any node whose surface trips the anti-poisoning sanitizer on
ingest is **quarantined per-tool**: the chain refuses that hop with a provenance reason rather
than calling it. The poison never reaches the agent. (Pegana is a *state* oracle — we present
state + exitability, never "sell now.")

---

## Publish gecko-surf as a Context7 library

> **Founder-run only.** The steps below *prepare* the listing artifacts. The actual
> **submission to Context7's registry is an outward action to a third party** — the founder
> submits it, no agent auto-submits. We stage the repo config and the metadata; a human
> presses go.

Context7 indexes open-source libraries so agents can discover their docs. Listing
**gecko-surf itself** is pure distribution: a developer starting on a new painful API finds
Gecko in the Context7 catalog, sees what it does, and reaches `gecko add`. No hosting, no
engine code, fully reversible.

### What the listing is

A catalog entry pointing at the public OSS repo, so Context7-connected agents can retrieve
Gecko's own docs the same way they retrieve any library's.

- **Repo URL:** `https://github.com/GeckoVision/gecko-surf`
- **Title:** `gecko-surf`
- **Blurb (what it does):** *Project the Agent Surface for any API — a deterministic,
  provenance-carrying, safety-checked call graph an agent traverses to get the call right
  the first time. Ingest an OpenAPI (or recover one from docs), get first-call-correct MCP
  tools, chain the calls agents get wrong, and verify doc claims against the live API before
  they fire. Apache-2.0.*
- **Getting started, one line:** `uvx --from "gecko-surf[serve]" gecko <openapi-url>`
  (or `gecko add <api>` to wire it into your agent).

### Why list it

Discoverability. Context7 is where a developer's agent already goes to ask *"what does this
library look like."* A gecko-surf entry means that when someone is fighting a long-tail,
paywalled, poorly-documented API, Gecko shows up as the tool that makes it agent-usable —
distribution to exactly our ICP, at zero marginal cost, reversible any time.

### The repo config — `context7.json`

Context7 reads a repo-level `context7.json` at the repository root to control how the
library is indexed. A draft is staged at the repo root: [`../context7.json`](../context7.json).

> **TODO (founder): confirm the schema against Context7's *current* `context7.json` spec
> before submitting.** The staged file uses the fields we're confident about
> (`$schema`, `projectTitle`, `description`, `folders`, `excludeFolders`, `rules`). Do not
> invent additional fields — verify against Context7's published schema, then submit.

### Submission steps (founder)

1. Confirm the staged [`../context7.json`](../context7.json) against Context7's current
   published schema (see TODO above). Adjust fields only to match their spec.
2. Ensure the repo is public: `https://github.com/GeckoVision/gecko-surf` (it is, Apache-2.0).
3. Submit the repo to Context7's "Add Library" / library-registry flow with the metadata
   above (repo URL, title, blurb).
4. After indexing, sanity-check that the catalog entry resolves to the OSS repo and the
   getting-started line renders.

Re-review is manual and each update is re-indexed — same as any Context7 library.
