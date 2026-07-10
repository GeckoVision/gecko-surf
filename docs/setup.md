# Gecko — setup guide (v0.3.0)

Get Gecko running, store your API keys safely, and point your agent at a surface.
Takes ~5 minutes. The only prerequisite is **`uv`** — no Python/pip juggling.

---

## 0. Install `uv` (one time)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
(Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

Everything below runs through `uvx`, which fetches Gecko from PyPI on demand — you
never install it globally.

---

## 1. Get / refresh to the latest version (0.3.0)

`uvx` caches, so to make sure you're on the newest release, pass `--refresh`:

```bash
uvx --refresh --from "gecko-surf[serve]" gecko --help
```

Confirm the version:
```bash
uvx --from "gecko-surf[serve]" python -c "import gecko; print(gecko.__version__)"
# → 0.3.0
```

> Installed it as a tool instead of using `uvx`? Update with `uv tool upgrade gecko-surf`.

---

## 2. Store your API keys — safely

Gecko keeps provider secrets in your **OS keychain** and injects them at call time,
**in your local runner**. The key never reaches the agent, never lands in shell
history, a dotfile, or a log — and it never leaves your machine.

```bash
# Store a key (hidden prompt — paste it when asked):
uvx --from "gecko-surf[serve]" gecko auth set <provider>      # e.g. ...gecko auth set colosseum

# List stored keys (NAMES only, never values):
uvx --from "gecko-surf[serve]" gecko auth list

# Confirm a key resolves (reports the backend, never the value):
uvx --from "gecko-surf[serve]" gecko auth test <provider>

# Remove one:
uvx --from "gecko-surf[serve]" gecko auth rm <provider>
```

CI/headless (no keychain)? Fall back to an env var, e.g. `export COLOSSEUM_COPILOT_PAT=...`.

---

## 3. Which do I use — local or hosted?

**This is the important part.** There are two ways to reach a Gecko surface, and the
choice is decided by *whose key the API needs*:

| | **Run it locally** | **Hosted (`mcp.geckovision.tech`)** |
|---|---|---|
| Use it for | Any API that needs **your** key — Colosseum, dpo2u, a private/paywalled API | The **public** surfaces we host (no key needed) |
| Your key | Stays in your keychain, injected on your machine | n/a — these need no key |
| URL | `http://127.0.0.1:8000/mcp` | `https://mcp.geckovision.tech/<name>/mcp` |

> **Why Colosseum must run locally:** Colosseum needs *your* personal token. Gecko's
> whole design is that we **never hold your key** — so a keyed, per-person API can't be
> a shared hosted surface. You run it locally and your token never leaves your machine.

---

## 4A. Local — a keyed API (e.g. Colosseum)

```bash
# 1. Store your token once (hidden prompt):
uvx --from "gecko-surf[serve]" gecko auth set colosseum          # PAT from arena.colosseum.org/copilot

# 2. Serve it (injects your key at call time, hidden from the agent):
uvx --from "gecko-surf[serve]" colosseum-mcp

# 3. Point your agent at it:
claude mcp add --transport http colosseum http://127.0.0.1:8000/mcp
```

Other keyless / bundled examples work the same way:
```bash
uvx --from "gecko-surf[serve]" jupiter-mcp        # Jupiter Swap — keyless (free tier)
```

**Any API by its OpenAPI URL:**
```bash
uvx --from "gecko-surf[serve]" gecko <openapi-spec-url>      # then claude mcp add ... http://127.0.0.1:8000/mcp
```

**Claude Desktop / Cursor** config instead of `claude mcp add`:
```json
{
  "mcpServers": {
    "colosseum": { "url": "http://127.0.0.1:8000/mcp", "transport": "streamable-http" }
  }
}
```

> **"Connected" but 0 tools?** Your MCP client runs in a different network sandbox than
> your shell (common with some harnesses). Serve behind a real URL:
> ```bash
> cloudflared tunnel --url http://127.0.0.1:8000
> uvx --from "gecko-surf[serve]" colosseum-mcp --public-url https://<name>.trycloudflare.com
> ```
> then `claude mcp add --transport http colosseum https://<name>.trycloudflare.com/mcp`.

---

## 4B. Hosted — a public surface (no key)

```bash
claude mcp add --transport http reportavnzla https://mcp.geckovision.tech/reportavnzla/mcp
```

The bare `https://mcp.geckovision.tech/mcp` is the **"submit an API to comprehend"**
tool, not a data API — point at `/<name>/mcp` for a specific surface.

---

## 5. Verify it's working

```bash
# Local server up?
curl http://127.0.0.1:8000/healthz        # → 200 / healthy

# Keys in place?
uvx --from "gecko-surf[serve]" gecko auth list
```

Then in your agent, ask it to use a tool from the surface. The first call should be
correct — right parameter names, no guessing.

---

## 6. OAuth APIs — auto-refresh (new in 0.3.0)

If an API uses OAuth (a short-lived token + a refresh token), Gecko now handles the
token lifecycle for you: it refreshes **before** the token expires and self-heals a
401 — the agent never sees an expired session.

You do the one-time interactive login yourself (Gecko can't do a human's 2FA); after
that, Gecko takes over. Example flow for a provider like dpo2u:
```bash
npx dpo2u-cli login          # browser + email OTP → saves ~/.dpo2u/oauth.json
# then serve dpo2u through Gecko — it reads that token file and refreshes on its own.
```

---

## Handy flags (for `gecko <spec>` / the examples)

| Flag | Default | What it does |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Bind port |
| `--public-url` | — | Advertise this URL (for tunnels) |
| `--allow-host` | — | Extra trusted host(s) for auth injection |

**Golden rule:** Gecko only ever injects your key toward the API's own host — it
refuses to leak a secret anywhere else.
