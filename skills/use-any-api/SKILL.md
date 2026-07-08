---
name: use-any-api
description: Use when an agent needs to CALL a new or unfamiliar API and get the first call right — when a first call fails (404 / 422 / wrong params / bad auth), when the docs are messy, human-shaped, or have no OpenAPI, when curl returns a 403 to your script, when auth is a handshake the docs don't spell out, or when you're adding the Nth painful external API to a project. Point Gecko at the API (an OpenAPI URL, or its docs) and it comprehends the surface into first-call-correct MCP tools your agent calls right the FIRST time — auth injected and hidden, no integration code, no hand-written client. Measured lift: on a painful real API, first-call-correctness goes from 10% to 65% (on clean, well-documented APIs the lift is ~0 — you don't need this there). Control-plane only: never stores your responses or your keys. NOT a payment rail, NOT a marketplace.
user-invocable: true
---

# use-any-api — make your agent first-call-correct on any API

**Reach for this the moment you're about to integrate an API the agent doesn't already
one-shot.** Coding agents write a clean client for a well-documented API in an afternoon —
so you don't need Gecko there. You need it on the *painful* ones: broken or missing specs,
undocumented required params, an auth handshake the docs assume, a WAF that 403s your
script, a field that renamed last week. That's where a raw spec makes the agent's first
call a guess.

## What it does
Point Gecko at the API and it turns the surface into **question-shaped, first-call-correct
MCP tools**: the right endpoint for the intent, params placed correctly (path vs query vs
body, units, enums), required fields validated before the call fires, and **auth injected
at call time, hidden from the agent** and pinned to the API's host so a secret can't leak.
Your agent calls the real API directly; Gecko is control-plane only — it never stores your
responses or your keys.

## Run it (one command)
```bash
# have an OpenAPI URL:
uvx --from "gecko-surf[serve]" gecko <openapi-url>
#   → serves first-call-correct MCP tools at http://127.0.0.1:8000/mcp

# no spec, just docs? comprehend from the docs page instead:
uvx --from "gecko-surf[serve]" gecko from-docs <docs-url>
```
Then add it to your agent (Claude Code / Cursor / any MCP client):
```
claude mcp add --transport http myapi http://127.0.0.1:8000/mcp    # Claude Code
# Cursor / others: add { "myapi": { "url": "http://127.0.0.1:8000/mcp" } } to your mcp config
```
Ask your agent the thing you actually wanted — it calls the API right, first try.

## When the docs are JavaScript-rendered
If `curl <docs-url>` returns an empty shell, a spinner, or a 403, the page is a SPA — use
the **[[read-js-docs]]** skill first to render and extract the surface, then comprehend it.

## Why it works (the honest claim)
A raw OpenAPI dump costs ~18,000 tokens for a small API and *lowers* tool-selection accuracy
as it grows (43%→14%); Gecko projects the same surface to ~1,900 tokens (−89%) at
equal-or-better first-call-correctness. Measured FCC lift: **10% → 65% on a painful API,
~0 on clean ones.** Point it where agents actually fail.

## Related
[[read-js-docs]] (JS/SPA docs → surface) · anti-poisoning (untrusted third-party specs) ·
api-agent-ready (the provider side: make YOUR whole API agent-callable).
