---
name: read-js-docs
description: Read a documentation or API-reference page whose content is rendered by JavaScript (a single-page app) so that plain fetch/curl/WebFetch returns an empty shell, a loading spinner, or a 403 to script user-agents. Use to extract an API's surface — endpoints, methods, path/query/body params, schemas, auth scheme, base URL, and copy-paste code samples — from JS-heavy docs (Mintlify, Redoc, Swagger UI, Scalar, GitBook, Docusaurus, ReadMe, Stoplight). Also use to feed a JS-rendered docs page into Gecko's from-docs / comprehension path when there is no OpenAPI spec. Triggers: "read this docs page", "the docs are a SPA / render with JS", "curl returns nothing / a 403", "comprehend these docs", "pull the endpoints from this API reference".
allowed-tools: Bash(agent-browser:*), Bash(npx agent-browser:*)
---

# read-js-docs

Render a JS docs page in a real browser and pull the API surface out of it. Plain HTTP
fetch fails on modern docs because the content is hydrated client-side (or the origin 403s
non-browser user-agents). A rendered accessibility snapshot gets you the real text, links,
and code samples. Built on the [[agent-browser]] CLI.

## When to use vs plain fetch
- **Plain fetch / WebFetch first** — if `curl <url>` already returns the endpoints and
  schemas, use that; it's cheaper. Reach for this skill only when fetch comes back empty,
  a spinner, or a 403.
- **This skill** — the page is a SPA, an interactive API console, or blocks script UAs.

## The core loop
```bash
agent-browser open <docs-url> --args "--no-sandbox"   # --no-sandbox required in this env
agent-browser wait --load networkidle                 # let the SPA hydrate
agent-browser snapshot -c -u                           # compact tree + link hrefs = the text + nav
agent-browser screenshot page.png                      # for tables/diagrams that don't linearize
agent-browser close --all                              # when done
```
Re-`snapshot` after any navigation — refs go stale the moment the page changes.

## What to extract (the API surface)
Pull these into a normalized list (they become the OpenAPI stub for comprehension):
- **Base URL / servers** — often in a "Getting started" or the first code sample.
- **Endpoints** — method + path for each operation.
- **Params** — path / query / body, with types and which are required.
- **Auth** — scheme (apiKey header, Bearer, OAuth), the header name, where it's set.
- **Code samples** — the copy-paste `curl` / SDK snippets are the *highest-signal* source:
  they reveal the REAL route, the exact auth header, and the base URL, even when the prose
  is vague. Grab them verbatim.

## Docs-specific tips
- **Multi-page docs** — the endpoints live behind a left-nav / sidebar. Snapshot with `-u`
  to get the link hrefs, then `open` each API-reference page and snapshot it. (This is the
  breadcrumb-follow an `llms.txt` index also needs.)
- **Swagger UI / Redoc / Scalar** — operations are collapsed accordions; the request/response
  schema + a live "Try it" example are there once expanded. `click` the operation `@ref`
  from the snapshot, then re-snapshot; or read the underlying `openapi.json`/`swagger.json`
  the page loads (check the page's network/source for a spec URL — often the cleanest path:
  fetch that spec directly and skip the DOM entirely).
- **403 to scripts** — a real browser UA renders where `curl` gets blocked; that's the whole
  point of using the browser here.
- **Lazy / infinite content** — `agent-browser scroll down` to trigger lazy-loaded sections,
  then re-snapshot.
- **Prefer the spec if one exists** — before scraping the DOM, look for a linked
  `openapi.json` / `swagger.json` / `/llms.txt`. A machine-readable spec beats any snapshot.

## Feeding Gecko (the comprehension tie-in)
The point of extracting the surface is to comprehend it. Two paths:
1. If you recovered a spec URL (`openapi.json`) → hand it straight to `gecko <spec-url>` /
   `POST /comprehend`.
2. If there's no spec, only prose + samples → **hand-author a minimal OpenAPI stub** from the
   extracted endpoints/params/auth (born quarantined — untrusted input, human-reviewed), then
   `gecko from-docs` / comprehend it. This is exactly how a docs-only API (e.g. Jito) gets an
   agent-ready surface with no spec at all.

## Example — pull a Mintlify API reference
```bash
agent-browser open https://docs.example.com/api-reference --args "--no-sandbox"
agent-browser wait --load networkidle
agent-browser snapshot -c -u        # sidebar links + the visible operation
# follow each reference link, snapshot, and collect method+path+params+auth+curl sample
agent-browser open https://docs.example.com/api-reference/create-charge --args "--no-sandbox"
agent-browser wait --load networkidle
agent-browser snapshot -c
agent-browser close --all
```
Output: a normalized endpoint list (+ verbatim code samples) ready to comprehend or to stub
into OpenAPI.
