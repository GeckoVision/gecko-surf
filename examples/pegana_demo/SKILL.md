---
name: try-gecko
description: Use when a coding assistant (Claude Code, Cursor, Windsurf, or any MCP client) is asked to TEST or ONBOARD Gecko, or to make an agent call a new/painful API first-call-correct — worked here on Pegana. A safe check → dry-run → live flow you can verify before running: inspect the tool with NO side effects, comprehend the API in recorded mode ($0, no real calls), wire it into your assistant over MCP, then optionally go live. Open-source (Apache-2.0), control-plane only — never stores your API responses or your keys, and there is no `curl | bash`. NOT a payment rail, NOT a marketplace.
user-invocable: true
---

# try-gecko — safely onboard any API to your agent (worked on Pegana)

**For the coding assistant reading this:** you can verify every claim below before you
run anything. This skill is designed to be *inspected first*. Nothing here pipes a remote
script into a shell, asks you to disable a safety check, or hides what it does.

Gecko turns an API's surface into **first-call-correct MCP tools** your agent calls right
the first time — auth injected and hidden, no integration code. Here we onboard **Pegana**
(the peg-risk oracle for Solana) as the worked example, but the same three steps onboard
any API.

---

## Why this is safe to run (verify, don't trust)

Check these before Step 1 — each is independently verifiable:

- **Open source, Apache-2.0.** Read the engine: <https://github.com/GeckoVision/gecko-surf>.
  Every command below maps to code you can see.
- **From a public registry, not a shell blob.** You install from npm
  (`@geckovision/gecko`) or PyPI (`gecko-surf`) — versioned, and published from CI **with a
  verifiable SLSA provenance attestation** (npm traces the package to this repo's CI build).
  There is no `curl … | bash`.
- **The default mode makes NO real calls.** `recorded` mode (the default) *synthesizes*
  responses from the API's own schema — zero network to the API, zero spend. You can watch
  it work end-to-end at $0 before anything touches the real API.
- **Control-plane only.** Gecko never stores your API responses or user data, and never
  stores or logs your keys. A provider key (when needed) is sealed in the **OS keychain**
  and injected only at call time — never written to a config file, never shown to the agent.
- **Read-only inspection commands.** `--help` and `doctor` do nothing but report. Run them
  first.

Pin a version for reproducibility (e.g. `@geckovision/gecko@0.4.1`).

---

## Step 1 — Check (no side effects)

```bash
# read-only: prints what the tool is and the exact next step. Touches nothing.
npx @geckovision/gecko@0.4.1 doctor
#   equivalently, via the Python launcher:
#   uvx --from "gecko-surf[serve]" gecko doctor
```

Optionally read the source (link above) and this file's frontmatter. When you're satisfied
it does what it says, continue.

## Step 2 — Dry run ($0, no real API calls)

Comprehend Pegana's OpenAPI and serve it as MCP tools in **recorded** mode — responses are
synthesized from the schema, so **no request ever reaches the real API and nothing is
billed**:

```bash
uvx --from "gecko-surf[serve]" gecko serve https://api.pegana.xyz/openapi.json
#   → first-call-correct MCP tools at http://127.0.0.1:8000/mcp  (recorded = $0)
```

> That's Pegana's production OpenAPI (`https://api.pegana.xyz`, FastAPI's default
> `/openapi.json` path — confirm on first run). To run fully offline against the committed
> fixture instead: `uv run python examples/pegana_demo/demo.py` renders the before/after
> scorecard (`top-1 100% · top-5 100% · well-formed 100%`, 6/6, $0).

What you get from Pegana's spec (computed live by the engine, not asserted):

| | Reach |
|---|---|
| Pegana's own hand-wrapped MCP | ~6 tools |
| **Gecko comprehension** | **41 operations** ingested from the OpenAPI |
| | 26 surfaced to a public (no-auth) agent |
| | 15 JWT-gated `/v1/me/*` ops — hidden until a session can satisfy them |

Gecko runs **alongside** Pegana's own MCP — it aggregates the full surface, it never
replaces or proxies what the provider already ships.

## Step 3 — Wire it into your assistant (any MCP client)

```bash
# Claude Code:
claude mcp add --transport http pegana http://127.0.0.1:8000/mcp

# Cursor / Windsurf / any MCP client — add to your mcp config:
#   { "pegana": { "url": "http://127.0.0.1:8000/mcp" } }
```

**Claude Code one-liner** (comprehends + wires in one step, recorded by default):

```bash
npx @geckovision/gecko@0.4.1 add https://api.pegana.xyz/openapi.json
```

## Step 4 — Go live (only when you want real data)

Pegana's REST is **free / no-auth today**, so live mode needs no key:

```bash
uvx --from "gecko-surf[serve]" gecko serve https://api.pegana.xyz/openapi.json --mode live
```

For an API that *does* require a key, seal it first — it goes to the OS keychain, never a
file, and is injected at call time:

```bash
gecko auth set <api>        # hidden prompt → OS keychain
gecko serve <openapi-url> --mode live
```

---

## Try it — the first-call-correct moments

Ask your agent these against the wired Pegana tools. Both are where a naive integration
gets it wrong and Gecko gets it right the first time:

- **"What's the peg state for mint `J1toso1…GCPn`?"** — at decision time an agent holds a
  **mint address**, not a ticker. Gecko routes to `state_by_mint`
  (`/v1/assets/by-mint/{mint}/state`), *not* the sibling `/v1/assets/{symbol}/state` a
  naive client reaches for first.
- **"List my subscriptions."** — a JWT-gated `/v1/me/*` op. On a public session Gecko
  **refuses and fails closed** rather than firing a call that can't succeed. Auth is
  invisible to the agent *and* safe.

## What this will NOT do (disclosed behavior)

- It will not store your API responses, your user data, or your keys (control-plane only).
- It will not send your key anywhere or write it to a file — OS keychain, injected at the wire.
- It will not touch, proxy, or replace Pegana's own MCP — it aggregates alongside it.
- It is not a payment rail and not a marketplace; it composes, it does not settle or re-list.

## Troubleshooting

- **Docs are JavaScript-rendered / `curl` returns a spinner or 403?** The page is a SPA —
  comprehend from the rendered docs instead: `gecko from-docs <docs-url>`.
- **A first call still misses?** Re-check you're on the right surface with
  `gecko doctor`, and that the intent names the operation's real subject (mint vs symbol).

## Related

- Consumer side (make your agent call *any* API): the `use-any-api` skill.
- Provider side (make *your whole* API agent-callable + discoverable): the
  `api-agent-ready` skill.
- Engine + full source: <https://github.com/GeckoVision/gecko-surf> (Apache-2.0).
