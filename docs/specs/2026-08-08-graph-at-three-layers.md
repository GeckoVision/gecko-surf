# The graph at three layers — and what a code graph taught us about API graphs

**2026-08-08.** Setup note + one finding worth keeping.

## The idea

The graph is the organizing principle at three layers of this company, with three
different objects:

| layer | object | tool | who runs it |
|---|---|---|---|
| our code | modules, functions, communities | Graphify (Apache-2.0, on-device) | any of us, locally |
| our team | lanes, shared state, feedback edges | `private/operating/` + the `coordinator` | the coordinator |
| **the product** | **API surfaces, joins, provenance** | **Gecko** | our users |

This is not a positioning change. We are still not an orchestrator and we do not run
anyone's graph. It is the cheaper claim and the more defensible one: **we use the
principle everywhere, and we sell it for exactly one object.** Show the graph; do not
claim to run yours.

## Setup

```bash
uv tool install graphifyy
graphify . --code-only              # local tree-sitter AST, no API key, no model call
```

`--code-only` is deliberate. Graphify reads non-code files with your configured model,
and this repo has `private/` (strategy, partner analysis, numbers). Code-only keeps the
whole run on-device.

**Scoped before it ran, not after.** `.graphifyignore` repeats what `.gitignore` already
excludes, because Graphify's gitignore-honouring default is one `--no-gitignore` flag away
from off — and `.graphifyignore` is prioritised precisely when that flag is passed.

**Scanned before it was installed.** Graphify ships skill files into every detected
assistant. That is the supply chain Skill Guard exists for, so all **135** of its
markdown files went through `gecko scan-doc` first: all CLEAN. Verified after the build
that no `private/` or `.env` source file reached the graph (554 distinct source files, 0
from either).

Result: **9,916 nodes · 24,359 edges · 324 communities** over 614 code files.

## The finding

We asked the graph about `auth_headers()` — the frozen adapter seam, the one thing
invariant #2 says the whole engine pivots on.

```
Node: .auth_headers()
  Source:    gecko/access.py L47
  Degree:    1
  Connections (1):
    <-- AuthSession [method] [EXTRACTED] gecko/access.py:L47
```

**Degree 1.** One edge, from the class that declares it. Yet the seam is called
throughout the caller, the client, and the MCP surface.

The reason is that `AuthSession` is a `typing.Protocol`. Callers bind to it *structurally*
— by shape, at runtime — so there is no static reference for an AST walk to follow. The
extraction is not wrong. It is complete with respect to what an AST can see, and the seam
is invisible to it **because we deliberately made it a protocol.**

## Why that matters to us

It is the same gap we sell against, in a different domain:

> An **AST** cannot see a protocol seam, because the binding is decided at runtime.
> An **OpenAPI spec** cannot see a request-time join, because the accounts belong to a
> route an aggregator computed a second earlier.

Jupiter's `route` declares 9 accounts; the landed instruction carries 25. The missing 16
are not a deficiency in that IDL — no IDL could carry them. Exactly so here: no AST could
carry the protocol edge.

This is the argument for the provenance ladder, arrived at from the outside. Graphify
tags every relation `EXTRACTED` / `INFERRED` / `AMBIGUOUS`; we tag `extracted` /
`recovered` / `flagged` / `cross_surface`. Two teams, different objects, both concluding
that a graph edge is worthless unless it says where it came from — and both needing a
tier that means *"structural extraction has a floor and this is below it."*

`cross_surface` is our name for that floor. Their `INFERRED` is theirs.

## What we take from them

1. **The floor is honest, not embarrassing.** Their `AMBIGUOUS` tier says "couldn't fully
   resolve" out loud. Same posture as our `flagged`.
2. **Opt-in-off for anything that records user intent.** Their query log is off by
   default, with a code comment reasoning that "a default-on record of proprietary
   queries contradicts graphify's on-device, no-telemetry posture." That is our
   categorical-corpus argument in someone else's words.
3. **On-device by default is a feature you can state plainly.** No account, no API key,
   nothing leaves the machine — a sentence, not a policy page.

## What we do not take

Their object. They graph **code**; we graph **API surfaces**. Running Graphify on our
repo is a good dev tool and an honest dogfood of the *idea*. It is not evidence for our
product claim, and the two should stay separate in anything outward-facing.
