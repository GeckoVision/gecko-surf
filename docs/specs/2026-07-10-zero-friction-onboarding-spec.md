# Zero-friction onboarding — stdio-first, `gecko doctor`, auto-tunnel, agent-setup

*2026-07-10 · owners: software-engineer (CLI) + devops-engineer (tunnel/transport) +
product-designer (output) · status: DESIGN → build Phase 1 now*

## The problem (observed, twice)

A teammate ran `colosseum-mcp`, got "11 tools ready," added `http://127.0.0.1:8000/mcp`,
and her agent showed **0 tools**. She then reached for a **cloudflared tunnel** — a public,
unauthenticated URL, an install, a two-terminal dance — to fix what is, for a single dev on
one laptop, a non-problem. The friction chain is:

1. Run a separate server (`colosseum-mcp`) in its own terminal.
2. Copy the printed `claude mcp add …` line into a second terminal.
3. Hit "0 tools" (the client's network namespace can't reach `127.0.0.1`).
4. Install + run cloudflared, capture a URL, re-run the server with `--public-url`, re-add.

Every step is avoidable.

## The root cause + the fix

The "0 tools" symptom is a **transport mismatch**, not a real requirement. Two MCP transports:

| Transport | How it connects | Localhost/tunnel issue? |
|---|---|---|
| **HTTP (Streamable)** | server runs standalone; client connects over the network | **Yes** — needs reachability → the tunnel dance |
| **stdio** | **client spawns the server** as a subprocess, talks over stdin/stdout | **None** — no port, no network, no tunnel |

Gecko **already has** `mcp_server.serve_stdio()` — but the examples (`colosseum.py`,
`jupiter.py`) and `gecko serve` default to **HTTP** (`serve_http`). For the overwhelmingly
common case — one developer, one laptop, a local client (Claude Code / Desktop / Cursor) —
**stdio is strictly better and removes the entire tunnel problem.** The one-liner becomes:

```bash
claude mcp add colosseum -- uvx --from "gecko-surf[serve]" colosseum-mcp --stdio
```

No separate serve terminal, no port, no tunnel, no "0 tools." HTTP+tunnel is reserved for
its actual use case: exposing a surface to a **remote or genuinely sandboxed** client.

## Scope — four components (build in this order)

### Component 1 — stdio transport, and make it the recommended local path (Phase 1, the win)
- Add `--stdio` to `gecko serve` and the bundled example entries (`colosseum-mcp`,
  `jupiter-mcp`, any `<name>-mcp`). It routes to `mcp_server.serve_stdio(surface)` instead of
  `serve_http`. Same comprehension, same auth injection (key still resolved + injected at call
  time in the local runner) — only the transport edge changes.
- The startup banner prints the **stdio add command as the primary recommendation**
  (`claude mcp add <name> -- <the exact spawn command>`), with the HTTP form demoted to
  "serving to a remote/shared client?".
- Auth still works: stdio spawns the same process, which resolves the keychain credential and
  injects it — the agent never sees it. (No new secret surface.)
- **Boundary:** a client that cannot spawn a local subprocess (a pure web/cloud client) can
  use neither stdio nor localhost-HTTP — it genuinely needs a remote endpoint, and for a
  **keyed** API that means the hosted surface can't help (we never hold the user's key). State
  this honestly; it's a real limit of that environment, not a Gecko gap.

### Component 2 — `gecko doctor [--json]` (Phase 2, the self-diagnosis)
One command that inspects the environment and prints the **exact** setup for it — human-
readable by default, `--json` for an agent to consume and act on. Checks:
- Is the `mcp` package importable (stdio/serve available)? If not, the install hint.
- Is a credential present for the surface (`auth list` / resolver probe — presence only)?
- Recommended transport: **stdio** unless the caller declares a remote client.
- HTTP mode only: is the port free? is `cloudflared` installed (for the `--tunnel` fallback)?
- **Output = a diagnosis + the one command to run.** `--json` returns
  `{checks:[{name,ok,detail}], recommended_transport, add_command, warnings}` so an agent can
  read it, fix what's missing, and add the server without a human in the loop.

### Component 3 — `--tunnel` auto-cloudflared (Phase 3, for the real remote case)
When HTTP serving to a remote client is genuinely needed, collapse the 2-step tunnel dance:
- `gecko serve <spec> --http --tunnel` (and `<name>-mcp --tunnel`): if `cloudflared` is on
  PATH, spawn it against the bound port, capture the `trycloudflare.com` URL, and wire it in
  as `--public-url` automatically (host trusted in one shot). Print the ready add command.
- If `cloudflared` is absent: one clear line to install it, or fall back to stdio.
- **Never** the default; always explicit. A public tunnel is a real exposure — opt-in only.

### Component 4 — agent-native setup (Phase 4)
Make the agent able to onboard itself:
- A `setup` skill / quickstart section: "run `gecko doctor --json`, read it, act." So an agent
  pointed at Gecko can diagnose + connect without a human debugging session (the exact class
  of problem Gecko fixes for *other* APIs — we should not have it ourselves).
- Fold the recorded-vs-live and local-vs-hosted rules into the doctor output + quickstart.

### Component 5 — the surface hub: one process, one registration, ALL your APIs (needs a staff-engineer design pass)
**The question that surfaced this: "10 APIs = 10 background processes?" — no, and we should make that impossible.** `http_server.py` already has a *centralization surface* (`serve_http(surfaces=[(name, spec_or_client), …])`, "serve MANY comprehended surfaces from one host") — one process, each API at `/{name}/mcp`. That's what the hosted server runs. Two levels of consolidation:

- **Level 1 — one process, N endpoints (small lift, reuses the centralization surface).** A local config of which surfaces the user wants (`~/.gecko/surfaces.toml`: `name → spec + credential-ref`, or reuse the registry `SurfaceStore`), and a `gecko hub` / `gecko serve --all` command that mounts them all in one process. 10 APIs → 1 process, still 10 `claude mcp add` lines. Kills the process sprawl.
- **Level 2 — one process, ONE registration, all tools (the ideal; a real architecture decision).** A single aggregate MCP endpoint exposing every configured surface's tools through one connection, tools namespaced per surface (`colosseum.search_projects`, `jupiter.QuoteGet`), with `search_capabilities` spanning all surfaces and per-surface credential injection. With **stdio** this is the cleanest: `claude mcp add gecko -- uvx --from "gecko-surf[serve]" gecko hub --stdio` → one spawned process, one registration, all the user's APIs, agent discovers across them.

Level 2 crosses lanes (registry/`SurfaceStore` + surface aggregation + tool-name namespacing + a local surface config + per-surface auth) → **route to `staff-engineer` for a design pass before building.** Open questions: namespacing scheme + collision handling; does `search_capabilities` rank across surfaces or per-surface; config format + how a credential-ref binds to a surface; token-budget of N surfaces' tool lists (ties the scale-projection / `list_tools` refs work). Sequence it after `gecko doctor` (Phase 2); Level 1 can ship earlier as a thin extension of the centralization surface.

## Also fold in (cheap, related)
- The `serve.py` self-diagnosis (already prints the cloudflared hint on the HTTP path) should
  **lead with "try stdio"** and the "wait ~20s / clear a stale registration" steps before the
  tunnel (matches the corrected docs).
- Add a **recorded-vs-live** note everywhere serve is documented: the demo/serve runs recorded
  ($0) unless a live session/credential is present; live is opt-in.

## Build plan — Pattern B, falsifier-first

1. **Phase 1 (now):** `--stdio` on `gecko serve` + the example entries; stdio add command as
   the primary banner. **Falsifier (offline, $0):** a test that `--stdio` routes to
   `serve_stdio` (injected/monkeypatched) with the correctly-built surface + session, does
   **not** bind a port, and that HTTP behavior is unchanged when `--stdio` is absent. The
   banner asserts the stdio spawn command is emitted.
2. **Phase 2:** `gecko doctor` + `--json`. **Falsifier:** doctor on a constructed env
   (mcp present/absent, cred present/absent) emits the expected checks + `add_command` +
   `recommended_transport`, deterministically, offline.
3. **Phase 3:** `--tunnel`. **Falsifier:** with an injected fake `cloudflared` that prints a
   URL, `--tunnel` captures it and sets `public_url`; absent-cloudflared path gives the clean
   install message. No real network.
4. **Phase 4:** the setup skill + docs. Verified by an agent-in-the-loop dry run.

## Invariants (unchanged)
- Auth still resolves + injects in the local runner; the key never transits Gecko's control
  plane and is never in a tool def, regardless of transport. stdio adds **no** new secret
  surface (the process is the same, just wired to stdin/stdout).
- Engine core (ingest/catalog/tools/caller/risk/enforce) untouched — this is transport + CLI
  ergonomics only.
- The tunnel is always opt-in and clearly flagged as a public exposure.
