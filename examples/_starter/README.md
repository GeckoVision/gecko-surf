# `_starter` — build an app on ANY API in ~20 lines

The smallest possible app on top of surfcall. No LLM, no API keys, no network —
it runs in **recorded mode** ($0) so you can prove an API is callable-correctly
before spending a cent or a token. Fork it for your own API.

## Run it

```bash
# 1. Embed the SDK (engine-only — no extras needed)
uv run python -m examples._starter.app <openapi-spec-url-or-path> "<what you want>" key=value ...

# Example — any public OpenAPI:
uv run python -m examples._starter.app \
  examples/sos_vzla_bot/spec/sosvenezuela_openapi.json \
  "cuántas personas desaparecidas hay" 
```

It prints the capability it chose for your intent and the (recorded) result
synthesized from the response schema.

## What it shows

```python
from surfcall import AgentApiClient, public_session

client = AgentApiClient(spec, session=public_session())
hit = client.search("what you want")[0]          # intent -> right endpoint
result = client.call(hit["name"], {...},          # correct request, first try
                     mode="recorded")              # "live" for real data
```

That's the whole contract: **comprehend → find → first-call-correct**.

## Make it real

| You want… | Change |
|---|---|
| Real data | pass a real `Session` (`surfcall.access`) + `mode="live"` |
| A paywalled API | a BYOK `Session` whose `auth_headers()` returns your creds — injected at call time, never in the tool defs |
| A full AI agent | wrap `client.search` / `client.call` in an LLM tool-use loop — see **[`examples/sos_vzla_bot/`](../sos_vzla_bot/)** (Telegram + Claude/OpenRouter, allow-list + sanitize + never-raise) |
| An MCP endpoint for Claude/Cursor | `uvx surfcall serve <openapi-url>` — zero code, prints the one-click add string |

surfcall stays **control-plane only**: it never stores the API's responses — your
app calls the upstream API directly for data.
