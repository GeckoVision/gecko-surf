# Quickstart — make any API agent-usable in minutes

Bring an OpenAPI URL (or point Gecko at the docs). Gecko reads the API's *surface*,
turns every endpoint into a question-shaped, first-call-correct tool, and wires it into
your agent — no client code, no key pasted into a config file. Prove every call offline
for **$0** before you spend a token or a cent.

<!-- 🎬 GIF: `gecko add <url>` → paste nothing → the agent lists tools and makes a correct first call. -->

## 1. Add any API to your agent

One command comprehends the API, seals your key in the OS keychain, and wires it into
Claude Code over stdio — straight from `npx`, no clone, no Python:

```bash
npx @geckovision/gecko add https://api.provider.com/openapi.json
```

It prompts once (hidden) for the key and injects it **live** at call time — the agent
never sees it. Keyless API? Same command, no prompt.

## 2. Ask your agent — it calls it right

```
You:   search the API for X
Agent: ✓ called GET /v1/search?q=X  →  200, first try
```

No integration code, no docs-diving, no key in sight. That's the whole loop.

## 3. Prove it offline first — `$0` recorded mode

Every path has a **recorded mode** that runs the *same code* but synthesizes the
response from the API's own schema — no network, no keys, no spend. Falsify the calls
before going live:

```bash
uv run python -m gecko.demo     # goal → discover → correct call → data (recorded, $0)
```

When you're ready for real data, flip one flag: `--mode live`. Same path, same tools.

---

## Other ways in

Pick the one that fits — you don't need more than one.

**In Claude Code — the plugin** (bundles the skills + a live demo surface):

```
/plugin marketplace add GeckoVision/gecko-surf
/plugin install gecko-surf@geckovision
/make-agent-ready https://api.example.com/openapi.json
```

**Serve over MCP** (Cursor, VS Code, any client) — prints the MCP URL + one-click add
strings, then serves the API:

```bash
uvx --from "gecko-surf[serve]" gecko https://api.example.com/openapi.json
```

**Embed the SDK** (your own app or agent loop):

```python
from gecko import AgentApiClient, public_session

client = AgentApiClient(spec, session=public_session())   # spec = URL, path, or dict
hit = client.search("what you want")[0]                   # intent → right endpoint
client.call(hit["name"], {...}, mode="recorded")          # "live" for real data
```

Forkable starter (any API, ~20 lines, $0): [`examples/_starter/`](../examples/_starter/).

**No OpenAPI?** Recover a draft spec from the docs, then comprehend it:

```bash
uv run gecko from-docs https://api.example.com/docs       # -o draft.json to keep it
```

Review the draft (especially auth) before trusting it live; a published `openapi.json`
is always better.

---

## Good to know

- **Spec hosted elsewhere than the API?** (e.g. Colosseum Copilot) — assert the real
  host yourself so Gecko's anti-poisoning rule can pin requests to it:
  `gecko add <spec-url> --base-url https://api.host.com --mode live`.
- **Gecko never holds your keys.** A provider's key is yours — sealed in *your* OS
  keychain, resolved only at call time. Control plane only: Gecko stores the API
  surface, never keys or response data. ([FAQ & governance](faq.md))
- **Recorded is the default.** The CLI serves `--mode recorded` ($0, synthesized)
  until you pass `--mode live`.
- **Remote / hosted MCP?** Serve behind an HTTPS tunnel with
  `--public-url https://<tunnel>` (trusted for the Host/Origin guard). Gecko also runs
  a hosted surface at `mcp.geckovision.tech`.
- **Still V2 (designed, not built):** a vectorized semantic index (today's catalog is
  lexical) and an auto-update job that re-comprehends an API when it ships a new
  version. ([Stay correct](stay-correct.md))

Next: [How it works](how-it-works.md) · [For providers](for-providers.md) · [Why Gecko](why.md)
