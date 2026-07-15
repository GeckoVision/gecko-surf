# Changelog

## 0.4.5 — 2026-07-14

### Added
- **Onboard ping (attribution).** `gecko add` emits one anonymous, control-plane-only
  event (API host, CLI version, OS, a random install id) to the hosted
  `/events/onboard` route — default-on with a printed transparency line;
  `GECKO_TELEMETRY=off` disables it entirely. Adopters finally become countable. (#137)
- **x402 live settlement client.** `HttpFacilitatorClient` (fail-closed, SSRF-validated,
  token-redacting) + `facilitator_from_env()` reading `X402_FACILITATOR_URL`,
  `X402_FACILITATOR_TOKEN`, `X402_PAY_TO`, `X402_ASSET`, `X402_NETWORK`.
  `X402_MODE=stub` remains the shipped default; the go-live sequence is documented in
  `docs/x402-go-live.md`. (#139)

### Fixed
- **Live mode on a multi-server spec fails closed.** `AmbiguousServerError` lists the
  spec's servers and asks for an explicit `base_url`/`--base-url` instead of silently
  calling `servers[0]` (often production — the money-API footgun). `gecko add --mode
  live` refuses up front on ambiguous specs; the hosted Jito provider surface is now
  pinned explicitly to mainnet. (#138)
- CLI copy: "wired" → "integrated" in the `add --mode` help text. (#136)

### Note
- 0.4.4 was an npm-only re-stamp release; PyPI stayed at 0.4.3. This release realigns
  npm and PyPI in lockstep.

## 0.4.3 — 2026-07-13

### Added
- **`gecko add <domain>` auto-discovers the spec.** When the ref isn't itself an OpenAPI
  document, `resolve_spec` probes common locations on the host (`/openapi.json`,
  `/swagger.json`, `/v1/openapi.json`, `/.well-known/openapi.json`, …) before falling
  back to docs recovery. Each probe is SSRF-validated and best-effort. So a dev can point
  `gecko add` at a bare domain, a docs page, or a spec — one command, any API — instead of
  hunting for an `openapi.json` a painful API probably doesn't publish. Direct spec URLs
  still short-circuit (no extra probing).

## 0.4.2 — 2026-07-13

### Added
- **Bundled example surfaces are now `gecko` subcommands** — `gecko jupiter-mcp` and
  `gecko colosseum-mcp` (previously only standalone console scripts). This gives them a
  zero-install path through the single `gecko` binary, so **`npx @geckovision/gecko
  jupiter-mcp`** (and `colosseum-mcp`) work with no Python and no local spec file. Lazy-
  imported, so they add nothing to `gecko add`/`doctor`.

## 0.4.1 — 2026-07-13

### Fixed
- **`gecko add` no longer crashes without a TTY.** For an API that declares auth, the
  hidden key prompt previously raised a raw `getpass`/`termios` traceback when run
  under an agent, in CI, or with piped stdin — the exact non-interactive contexts our
  agent-first users onboard in. It now degrades gracefully off a TTY: no key is read,
  the surface still comprehends and wires (recorded/$0 needs no key), and the CLI
  prints the documented "add later with `gecko auth set`" hint. The secret is never
  echoed or logged.

## 0.3.0 — 2026-07-10

Governance + sessions. This release turns Gecko from "call the API correctly" into
"call it correctly **and** govern what the agent does" — plus real handling for the
short-lived-token auth pattern most production APIs use.

### Added
- **Governance tier + policy gate** — a deterministic classifier reads whether an
  operation is a `read` / `write` / `transfer` from the parsed spec (money-verb
  lexicon + amount∧recipient co-occurrence). An operator-authored `AgentPolicy`
  (`spend_cap` + `recipient_allowlist`) blocks a call **only** at the intersection
  with `tier == transfer` — a steered over-cap/off-allowlist transfer is refused
  while a benign read/write only ever steps up. Tier feeds `score_call` as a
  reason; it is never a blocking signal on its own.
- **Session identity** — `SessionIdentity` binds a session to its `AgentPolicy` and
  a non-secret free-tier id (shape-now-token-later); `GovernedSession` wraps any
  session and returns byte-identical wire headers (policy rides out-of-band).
- **Session lifecycle — token refresh + self-heal** — for OAuth-style APIs with a
  short-lived access token + refresh token: a `RefreshableSession` refreshes
  proactively inside `auth_headers()` before expiry, and a bounded-once reactive
  self-heal retries a 401'd live call after re-authenticating. `OAuth2Lifecycle`
  refreshes via a `refresh_token` grant; `oauth2_from_dpo2u()` reads a local OAuth
  token file. All behind the frozen `AuthSession` seam — a plain session is
  byte-identical.
- **Bundled Jupiter Swap API example** — `uvx --from "gecko-surf[serve]"
  jupiter-mcp`. Keyless by default (free tier), optional `JUPITER_API_KEY` (Pro)
  injected at call time.
- **BM25F retrieval** — Okapi BM25F with OpenAPI-remapped field weights; adopted
  above ~50 operations where it lifts recall (gate-confirmed on a 159-op surface),
  a no-op below.

### Fixed
- **Comprehension summary on fully-gated APIs** — an API where every operation is
  behind a bearer token reported `0` usable tools (the served, auth-filtered view).
  It now reports the full comprehended surface with an honest "N tools require
  authentication — Gecko injects the credential at call time" warning.

## 0.2.0 — 2026-07-03

The first release since the MCP-Registry launch. Everything below is on PyPI for
`uvx --from "gecko-surf[serve]" gecko ...` and the Claude Code plugin.

### Added
- **Agent-native emit** — any comprehended API gets its own discovery surface:
  `llms.txt`, `gecko.json`, `/.well-known/gecko.json`, `tools.md`, generated from
  the comprehended surface (control-plane only). Served as routes on the MCP
  server and writable for provider hand-off via `gecko <spec> --emit-dir <dir>
  [--site-url ...]`. Every emitted field is sanitized (anti-poisoning +
  secret-shape redaction + markdown neutralization); the capability map lists
  usable operations only.
- **`gecko test`** first-call-correctness suites and **`gecko from-docs`**
  (recover a draft OpenAPI from a human doc page) are documented as shipped.
- **Usage events** (`[events]` extra, `gecko/events.py`) — control-plane
  `surf.search` / `surf.prepare` / `surf.call` metadata with a closed field
  allowlist. **No-op unless `MONGODB_URI` is set** — a plain install never
  phones home; `GECKO_TELEMETRY=off` hard-disables.
- **Dense-hybrid retrieval arm** (`[dense]` extra) — MongoDB Atlas `autoEmbed`
  dense search fused with the lexical catalog (RRF). Benchmark-only for now;
  the agent-facing `search()` is unchanged.
- **Correctness-corpus provenance rails** — call outcomes carry `source`
  (`observed` / `reported` / `synthetic`); synthetic (recorded-mode) outcomes are
  segregated and never counted in first-call-correct metrics.

### Fixed
- **Below-scale surfacing:** on surfaces ≤50 operations, `search()` now surfaces
  every usable tool instead of top-k truncating — Gecko is now strictly ≥ a raw
  spec dump on small APIs (this was a real first-call-correct regression on
  clean APIs).
- Recorded-mode outcomes no longer fabricate HTTP 200s into correctness metrics.
- Dockerfile: fixed the stale pre-rename package path and bundled the `events`
  extra so the hosted image can emit.

### Changed
- License references unified to **Apache-2.0** across the repo (the license
  itself was already Apache-2.0).
- Hosted-deploy account identifiers moved out of the repo into env config.

## 0.1.1 — 2026-07-01

- MCP Registry release: `mcp-name: tech.geckovision/surf` ownership marker in the
  PyPI description; `server.json` published to registry.modelcontextprotocol.io.

## 0.1.0 — 2026-06-29

- First public release: comprehend an OpenAPI 3.x → question-shaped,
  first-call-correct MCP tools; hidden auth; `$0` recorded mode; Streamable-HTTP
  serve with one-click add strings; SSRF guard; anti-poisoning defenses.
