# Gecko CLI — v1 Design

**Date:** 2026-07-12
**Status:** Approved (design phase) → writing implementation plan
**Owner:** founder + software-engineer (engine) + devops-engineer (distribution)

## Purpose

A branded, `npx`-installable command-line tool that delivers Gecko's one-line
promise: **point it at any API and your agent can use it — first-call-correct,
with the key sealed in the OS keychain and never pasted into a client config.**

It is the **discovery instrument** for dev conversations starting the week of
2026-07-14. Success is measured by what those conversations reveal, not by
feature count:

- Does one-command onboarding land as an "oh, nice" moment?
- Do devs care that the key is never exposed to the model / to `mcp.json`?
- Which APIs do they actually reach for first?

## The golden path (the whole point)

```
$ gecko add https://api.stripe.com/openapi.json
  ✓ comprehended 47 endpoints → first-call-correct tools
  ✓ key → sealed in OS keychain (never in mcp.json)
  ✓ added to Claude Code  (local stdio — no tunnel, no port)
  → ask your agent: "refund the last charge for customer X"
```

`gecko add <api>` is the hero command. `<api>` accepts an OpenAPI URL, a docs
page (via the existing `from-docs` recovery), or a known-name shorthand.

## Non-goals (v1)

- Hosted / multi-tenant serving (the hosted surface already exists separately).
- OAuth browser-based key flows (keychain + hidden prompt is enough for v1).
- Clients beyond Claude Code (Cursor/others are a fast-follow).
- Windows and mac-x64 binaries (arm64-mac + linux-x64 ship first).
- Payment / x402 features (that is the demo repo's concern, not this CLI).

## Architecture

Two layers, each with a single clear responsibility.

### 1. Engine layer (Python — extends what exists)

The comprehension engine, `auth` (keychain BYOK), `from-docs`, `serve` (stdio
MCP), and `doctor` already live in `gecko/cli.py` and the `gecko` package. v1
adds **one new command** that is glue over those pieces:

**`gecko add <api> [--name NAME] [--client claude] [--no-serve]`**

Flow:
1. **Resolve** `<api>` → an OpenAPI dict:
   - an `http(s)` URL ending in a spec → fetch (SSRF-validated) + ingest;
   - a docs page → `from-docs` recovery → ingest;
   - a known-name shorthand → a bundled/registry spec.
2. **Comprehend** → `Operation`/tool defs (existing `ingest` + `tools`). Report
   the endpoint/tool count.
3. **Key** (if the spec declares auth): prompt (hidden) and store via the
   existing `auth set` path (keychain). Never echo, never write to a dotfile or
   client config. If no auth is declared, skip.
4. **Configure the client** (default Claude Code): write/patch the MCP config so
   the client launches the surface over **stdio** (`gecko serve <ref> --stdio`),
   i.e. the client spawns the server — no tunnel, no port. Idempotent.
5. **Print next step**: the exact "ask your agent: …" line, plus how to remove
   it (`gecko rm <name>`).

Supporting commands (mostly existing, lightly polished):
- `gecko auth set|rm|list` — keychain BYOK (exists).
- `gecko doctor [--json]` — self-diagnosis (exists; light polish).
- `gecko rm <name>` — remove a client config entry (new, small).
- `gecko list` — show configured surfaces (new, small).

The engine layer stays **control-plane only** — no response payloads, no secrets
on disk outside the OS keychain, auth never logged.

### 2. Distribution layer (new — devops)

Ship the Python CLI as a native `npx` package, the esbuild / pay.sh way:

- **Build:** PyInstaller (or equivalent) produces a **single standalone binary**
  per platform in CI (GitHub Actions matrix). v1 targets: `darwin-arm64`,
  `linux-x64`.
- **Publish:** each binary is its own npm package
  (`@geckovision/gecko-darwin-arm64`, `@geckovision/gecko-linux-x64`), listed as
  `optionalDependencies` of a thin launcher package **`@geckovision/gecko`**.
- **Launcher:** a tiny Node `bin` that resolves the correct platform package and
  `execFileSync`s its binary, forwarding argv/stdio/exit code. No Python, no uv
  on the dev's machine — only Node (for `npx`).
- **Versioning:** launcher + platform packages share one version; a release tag
  builds all binaries and publishes them together (RELEASING.md-style lockstep).

Package name: **`@geckovision/gecko`** (scoped). Confirm unscoped `gecko` /
`gecko-cli` availability opportunistically; scoped is the default.

## Branding

- A **GECKO ASCII wordmark** banner (brand blue `#146EF5`), shown on bare
  `gecko` / `gecko --help`, in the pay.sh layout: wordmark, one-line tagline
  ("make any API agent-usable — first call correct"), then **grouped commands**
  (e.g. *Onboard* → `add`, `rm`, `list`; *Keys* → `auth`; *Diagnose* → `doctor`;
  *Advanced* → `serve`, `from-docs`, `test`).
- Tight, quiet output on success (checkmarks); numbered, actionable errors.
- Consistent voice: direct, code-first.

## Data flow

```
dev ── gecko add <api> ──▶ [engine binary]
                              resolve → ingest → comprehend
                              (auth? → hidden prompt → OS keychain)
                              write Claude Code MCP config (stdio launch)
                           ◀── "✓ … → ask your agent: …"

later: Claude Code spawns `gecko serve <ref> --stdio`
       → agent lists tools → calls one → engine injects the key
         host-pinned at call time → real API
```

## Error handling

- Unreachable / non-spec URL → clear message + `from-docs` suggestion.
- Spec quarantined by anti-poisoning → report + recorded-only note (existing).
- Client config not found / unwritable → print the exact manual `claude mcp add`
  line as a fallback (never fail silently).
- Missing platform binary (launcher) → actionable message with the supported
  platforms + a link.
- Auth prompt: never echo; Ctrl-C aborts cleanly; nothing persisted on abort.

## Testing

- **Engine:** targeted `pytest` for `gecko add` — resolve (URL/docs/name), the
  config-writer (idempotent; correct stdio launch entry), auth wiring (key goes
  to keychain, never to config), and the no-auth path. Light fakes for fetch +
  keychain; no network in unit tests. `mypy gecko` clean; `ruff` clean.
- **Launcher:** a smoke test that the Node launcher resolves a platform package
  and forwards argv/exit code (fake binary in test).
- **End-to-end (manual, pre-demo):** `npx @geckovision/gecko add <real api>` on
  mac-arm64 + linux-x64 → tools appear in Claude Code → agent makes a
  first-call-correct call. This is the "wired ≠ reaches the agent" check.

## Scope for next week (MVP)

1. `gecko add <api>` — the golden path (resolve → comprehend → key → configure
   Claude Code stdio).
2. Key injection wired into `add` (prompt if the spec declares auth).
3. GECKO banner + grouped help.
4. npm distribution: PyInstaller binaries (darwin-arm64, linux-x64) + launcher +
   optionalDependencies + release CI.
5. Light `doctor` polish; `gecko rm` / `gecko list` (small).

Out of scope (fast-follow): Windows/mac-x64 binaries, Cursor + other clients,
OAuth flows, hosted serving, payment features.

## Open decisions (resolved)

- Golden path = **onboard any API to your agent** (not key-first, not
  correctness-first). ✅
- Distribution = **bundled per-platform binary via npm** (pay.sh model), not a
  uv-bootstrapping Node wrapper. ✅
- Build on the **existing Python `gecko` CLI**, not a new codebase. ✅
