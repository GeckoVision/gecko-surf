# Step 1 — Comprehend the OpenAPI/docs → first-call-correct tools

**Status: Live.** This is the shipped core of `gecko-surf`.

The provider has an API surface (an OpenAPI 3.x doc, or human docs). The goal is to
turn that whole surface into question-shaped, first-call-correct agent tools —
without hand-wrapping endpoints.

## The one command

```bash
pip install "gecko-surf[serve]"

gecko https://api.example.com/openapi.json      # comprehend + serve (== gecko serve <spec>)
```

Zero-install alternative:

```bash
uvx --from "gecko-surf[serve] @ git+https://github.com/GeckoVision/gecko-surf" \
  gecko https://api.example.com/openapi.json
```

`gecko` ingests **every** operation in the spec (`$ref` resolution, cycle/depth
guarded), builds a lexical intent→endpoint catalog, and generates one question-
shaped tool per operation with **auth hidden**. It also synthesizes a
`search_capabilities` tool so an agent can map a natural-language intent to the
ranked endpoints that answer it.

## What "first-call-correct" means here

The engine resolves the four things one-shot agents get wrong:

| Job | What the engine does |
|---|---|
| **Discovery** | Lexical intent→endpoint catalog + the `search_capabilities` tool |
| **Access & Auth** | Auth headers hidden from the tool def, injected at call time |
| **First-call-correct** | `prepare()` resolves path/query/body placement + a schema-correct example |
| **Drift** | Re-ingest the current spec → regenerated tools, no hand-patching *(Building)* |

## No OpenAPI? Recover one from the docs

If the provider only has a human doc page, recover a draft spec first, then
comprehend it:

```bash
gecko from-docs https://docs.example.com/api      # recover a draft OpenAPI, then comprehend
```

Treat the recovered spec as a **draft** — review it before serving. All ingested
spec/doc content is untrusted input; the engine SSRF-guards every fetch (block
private IPs, loopback, link-local, `file://`).

## Use it from Python (when you want the jobs as library calls)

```python
from gecko.client import AgentApiClient

client = AgentApiClient("https://api.example.com/openapi.json")   # URL, path, or dict

hits  = client.search("get an asset's peg state by mint", limit=5)   # discovery
tools = client.list_tools()                                          # question-shaped, auth hidden
req   = client.prepare(hits[0]["name"], {"mint": "<mint>"})          # first-call-correct shape
res   = client.call(hits[0]["name"], {"mint": "<mint>"}, mode="recorded")   # $0, offline
```

**Two modes, one code path.** `recorded` synthesizes a schema-correct example
offline (no key, no spend — prove the shape first); `live` issues the real request.
They differ only at the transport edge, so a call that shapes up in `recorded` is
the same call `live`. Prove it offline first (Pattern B).

## Prove the comprehension before you ship it

`gecko test <spec>` generates and runs first-call-correctness checks and can emit a
standalone pytest module to commit to CI — so a spec drift that breaks a tool fails
in *your* CI, not in an agent's session.

```bash
gecko test https://api.example.com/openapi.json --out tests/test_agent_ready.py
```

## Control-plane only

The engine stores the API *surface* + generated tool defs + correctness metadata.
It **never** stores response payloads, user data, or secrets. That's the promise
that lets you onboard an API unilaterally — protect it.

Next: [artifacts.md](artifacts.md) — emit the breadcrumbs agents use to find the
comprehension.
