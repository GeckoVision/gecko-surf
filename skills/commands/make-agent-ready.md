---
description: "Make a provider's API agent-ready — comprehend its OpenAPI/docs with gecko, serve the FULL surface over MCP with a one-click add, emit the discovery breadcrumbs, and leave the provider's own MCP intact. Usage: /make-agent-ready <openapi-or-docs-url>"
---

# /make-agent-ready

Run the five-step onboarding spine on a provider's API and emit the served MCP + the
one-click add + the discovery breadcrumbs. Aggregate, not replace.

**Argument:** an OpenAPI 3.x URL, a docs page URL, or a pasted spec.

## Steps

1. **Comprehend.** If it's an OpenAPI URL, run `gecko <url>`. If it's a docs page,
   run `gecko from-docs <url>` to recover a draft spec first, then comprehend.
   Report the engine's live counts: operations ingested / tools surfaced / auth-gated
   hidden. (See `skills/api-agent-ready/comprehend.md`.)
2. **Serve MCP.** `gecko serve` stands up a Streamable-HTTP MCP for the full surface.
   Hand over the one-click add — `claude mcp add --transport http <name> <url>` plus
   the Cursor / VS Code deeplinks. (See `skills/api-agent-ready/serve-mcp.md`.)
3. **Emit breadcrumbs (Building).** Hand-author `llms.txt` + `gecko.json` (and
   optional `x-gecko` spec annotations) for the provider to drop at their origin.
   State clearly that `gecko` does not auto-emit these yet. (See
   `skills/api-agent-ready/artifacts.md`.)
4. **Make discoverable.** Breadcrumb-based, provider-hosted — not a public catalog.
   (See `skills/api-agent-ready/discoverable.md`.)
5. **Confirm aggregate-not-replace.** State explicitly that the provider's own MCP is
   untouched and runs side by side. (See
   `skills/api-agent-ready/aggregate-not-replace.md`.)

## The engine

```bash
pip install "gecko-surf[serve]"
gecko https://api.example.com/openapi.json      # comprehend + serve
# or, zero-install:
uvx --from "gecko-surf[serve] @ git+https://github.com/GeckoVision/gecko-surf" \
  gecko https://api.example.com/openapi.json
```

Prove correctness before shipping: `gecko test <spec> --out tests/test_agent_ready.py`.

## Notes

- **Aggregate, not replace** — never modify or shut down the provider's own MCP.
- **Control-plane only** — store the surface + correctness metadata, never payloads
  or secrets. SSRF-guard every fetch; treat spec/docs as untrusted input.
- **Be honest** — comprehend + serve are Live; artifact auto-emission,
  discoverability, drift, and the corpus are Building. Don't overclaim.
- To charge agents for calls, continue with `/setup-x402 <api>`.
