# Colosseum Copilot — ready-to-use, via Gecko

Give your coding agent **first-call-correct** access to the [Colosseum Copilot API](https://docs.colosseum.com/copilot/api-reference)
— search projects, analyze cohorts, compare, submit feedback — without wrestling the docs.

Gecko comprehended this API from its docs (Colosseum publishes no OpenAPI). Your token is
**BYOK**: injected at call time, hidden from the agent, sent only to Colosseum, and it never
leaves your machine.

> **No `curl | sh` anywhere.** Every step is a plugin from a repo you can read or a versioned
> package — nothing to blind-execute.

## 1 · Get Gecko

**Claude Code** (the default):
```
/plugin marketplace add GeckoVision/gecko-surf
/plugin install gecko-surf
```
**Any other client:** nothing to install first — the `uvx` step below fetches it.

## 2 · Run this surface (BYOK)

**No files to download** — the surface ships inside the package:
```bash
export COLOSSEUM_COPILOT_PAT=...            # get one: https://arena.colosseum.org/copilot
uvx --from "gecko-surf[serve]" colosseum-mcp
```
Serves **11 first-call-correct tools** at `http://127.0.0.1:8000/mcp`. Your PAT stays local.

> Gecko needs Python 3.11+. If your system Python is older, let uvx fetch one:
> `uvx --python 3.11 --from "gecko-surf[serve]" colosseum-mcp`

### If your agent runs in a different network context (sandboxed harnesses, containers, remote)

The default bind is loopback, which assumes your MCP client and this server share a network
namespace. Some agent harnesses don't — the symptom is `claude mcp list` reporting
**Connected** while the session loads **zero tools**. Serve behind a real URL instead:

```bash
cloudflared tunnel --url http://127.0.0.1:8000        # prints https://<name>.trycloudflare.com
colosseum-mcp --public-url https://<name>.trycloudflare.com
claude mcp add --transport http colosseum https://<name>.trycloudflare.com/mcp
```

(`--host`, `--port`, and `--allow-host` are also available — same flags as `gecko serve`.)

## 3 · Point your agent at it

**Claude Code / Cursor / Cline** (CLI):
```
claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp
```
**Any MCP client, via config** (`~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`, …):
```json
{ "mcpServers": { "colosseum": { "url": "http://127.0.0.1:8000/mcp" } } }
```

Then ask your agent: *"search Colosseum for Solana data-API projects"* — it calls it right, first try.

## Why it's not just `from_openapi`

There's no OpenAPI to convert. And even the docs mislabel the routes (they show
`/colosseum_copilot/status`; the real route is `/status`). Gecko reads the docs, comprehends
the true surface, hides the auth, pins the host so your PAT can't leak, and sends a real
User-Agent so Colosseum's WAF doesn't block it. That's the difference between *wrapping a spec*
and *making the call correct*.

Built with [Gecko](https://geckovision.tech) — the API comprehension layer for agents.
