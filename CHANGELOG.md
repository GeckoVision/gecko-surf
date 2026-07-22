# Changelog

## Unreleased

## 0.4.14 — 2026-07-22

### Security
- **Self-service login granted ACCESS, not just identity.** `store_key` set
  `enabled=True` unconditionally and `gecko login` mints through that same call, so any
  address that passed the email OTP received a key the gate accepted — every gated
  (paid) surface was reachable by anyone who could receive email. `keyauth.authorize`
  already documented "not enabled BY THE FOUNDER ⇒ deny"; login silently made that
  untrue. Login-minted keys now land `enabled=False`: login establishes identity,
  access stays a deliberate founder act. Founder-run `gecko keys mint` still lands
  enabled — that IS the grant.
- **One key opened EVERY gated surface.** `KeyGate.decide` took no surface argument, so
  "enabled" meant "may reach every paid API". Invisible with a single gated surface, but
  adding a second would have silently handed every developer enabled for API #2 access
  to API #1. The gate is now scoped per mount, and access requires two independent
  switches: `enable/disable` (the account is live at all) AND `grant/revoke` (it may
  reach THIS surface). Fail-closed throughout — a store that cannot express grants
  denies rather than degrading to a bare enabled check, grants default-deny, and
  `disable` still beats a surviving grant.

  **Migration:** existing keys carry no grant and therefore reach nothing. Restore
  access explicitly with `gecko keys grant <account> --surface <name>`.

### Added
- **`gecko connect <surface>`** — use a gated hosted surface with the Gecko key held in
  the OS keychain, so no secret is pasted into an MCP client config. It runs as the
  client's stdio MCP server, resolves the key through the normal credential chain
  (keychain → env), and bridges JSON-RPC frames verbatim to the hosted
  Streamable-HTTP mount. The client config holds a command, not a credential:
  `{"command": "gecko", "args": ["connect", "birdeye"]}`. Surface names are validated
  as mount names (no path traversal) and `--host` goes through the SSRF guard, so a
  bearer token can never be sent to loopback, a private range, or link-local.
- **`gecko keys grant|revoke <account> --surface <name>`** and `gecko keys mint
  --surface` (mint + grant in one act). `gecko keys list` now shows each account's
  grants.
- **`gecko serve` first-run ping** (`mode="serve"`) — the skill/plugin install
  channel (`/make-agent-ready` runs serve) becomes visible. Same envelope, same
  `GECKO_TELEMETRY=off` opt-out, same transparency line (on stderr — stdout may be
  the stdio JSON-RPC channel), fired once per install+surface, never per boot. The
  hosted ingest accepts `mode="serve"` in the same change (client/server lockstep).
- **`plane` event field** (`engine`|`surface`, closed set) on `surf.prepare` /
  `surf.first_call_correct` (engine) and `surf.call` (surface), documenting why
  all-time fcc > call is expected: fcc fires on every engine call outcome (demo,
  `gecko test`, recorded $0 flows included); `surf.call` only on MCP-surface
  invocations. The honest funnel queries are documented in `gecko/events.py`.

### Fixed
- **A rejected key hung the MCP client.** The transport's HTTP 403 escaped `gecko
  connect` as a raw ExceptionGroup: the process died with no JSON-RPC response and the
  client waited on `initialize`. Transport failures are now mapped to one redacted line
  naming the status and the remedy (exit 2, stdout left byte-clean because it is the
  protocol channel).
- **The documented headless credential fallback was unusable.** Slot `gecko-identity`
  produced `GECKO_CRED_GECKO-IDENTITY`, which no POSIX shell can `export`. `_env_key`
  now normalizes `-` to `_`; the pre-normalization name is still honoured on read, so a
  Docker `-e` or an MCP client `env` block that sets it does not regress.
- **Adoption telemetry counts adopters, not runs.** The install id is written
  atomically (temp + rename, never a torn file) and reused forever; an unwritable
  HOME degrades to a per-run id instead of crashing. The `gecko add` onboard ping is
  now idempotent per install+surface (a local `~/.gecko/pinged/` marker, written only
  after a ping actually left), so re-running `add` no longer re-counts. The ping URL
  is resolved at call time, so a dev/test harness that redirects it can no longer
  post into the production ingest. The test suite is structurally unable to post
  telemetry (suite-wide transport kill-switch).

## 0.4.13 — 2026-07-19

### Added
- **Chain plans reach the agent** — `search_capabilities` now attaches a `plan`
  block (ordered supplier steps + provenance-carrying `explain`) to the top hit
  when its required inputs aren't satisfiable from the stated intent. The agent
  gets the right *sequence* of calls first try instead of discovering it by
  trial and error; flat search is byte-identical when no chain is needed. Backed
  by the chain-FCC harness (`gecko/chain_eval.py`): both known TxLINE chains
  score first-plan-correct in recorded mode ($0).

## 0.4.12 — 2026-07-19

### Fixed
- **`npx @geckovision/gecko jupiter-mcp` / `colosseum-mcp` crashed** — the
  PyInstaller onefile binary did not bundle `gecko/examples/*.json|yaml`, so bundled
  surfaces raised `FileNotFoundError` on the npx channel (`txline-mcp` survived via
  its raw-URL fallback). Added `--collect-data gecko` to the build (~128 KB, no code
  change) so all three bundled surfaces work offline in the frozen binary.
- **`gecko auth test --live` could report ✓ on a call that never hit the wire** — it
  classified on HTTP status alone, but a `mode="live"` call silently degrades to
  recorded (quarantined / auth-unsafe surface) and returns a synthesized 200. It now
  treats any non-live run mode as inconclusive — the exact false-confidence `--live`
  exists to prevent.

## 0.4.11 — 2026-07-19

### Added
- **`gecko auth test --live`** — proves a credential actually *authenticates* (one
  safe auth-gated GET → HTTP status), not just that the keychain resolves a value. A
  resolvable-but-expired token now reports ✗ instead of a misleading `resolved ✓`.
  Auto-targets bundled surfaces (`txline`); `--spec`/`--base-url`/`--op` for any API.

## 0.4.10 — 2026-07-19

### Added
- **Bundled `txline-mcp` surface** — serve the TxLINE (TxODDS) API with no local spec
  file and no spec URL (`npx @geckovision/gecko txline-mcp --mode live --stdio`).
  Two-token auth sealed via `gecko auth set txline --account httpAuth|apiKeyAuth`.

## 0.4.9 — 2026-07-19

### Added
- **Multi-scheme (two-token) auth injection** — `gecko serve --auth-keychain` now
  injects *every* header-shaped security scheme a spec declares (e.g. TxLINE's
  `Authorization: Bearer` + `X-Api-Token` together), not just the first. Spec-driven
  and API-agnostic; single-scheme APIs are unchanged. `gecko serve` prints the exact
  per-scheme `gecko auth set` commands for a multi-token surface.

## 0.4.8 — 2026-07-16

### Added
- **`gecko login`** — hosted-identity enrollment via Privy passwordless email-OTP
  (email → one-time code → sealed credential). Gates only the hosted plane; local
  `gecko add` (recorded, $0) stays zero-login. Client-side flow, public app id only —
  the Privy app secret never enters the CLI. (#148)

### Fixed
- **Cloudflare `1010` User-Agent ban.** Outbound provider calls now send a real
  `gecko-surf/<version>` User-Agent instead of the default `Python-urllib/*`, which
  Cloudflare-fronted providers (e.g. Privy) reject with HTTP 403. (#148)

## 0.4.7 — 2026-07-15

### Fixed
- **macOS TLS blocker.** Frozen (npx) binaries bundle `certifi` and point the SSL
  context at it at startup, so every https call works out of the box — no more
  `CERTIFICATE_VERIFY_FAILED` on a clean Mac. (#143)
- **`gecko add <bare-domain>`** resolves via `https://` + spec discovery instead of
  treating the domain as a local file. (#142)
- **`gecko --version`** added at the top level (was falling into the serve parser). (#142)
- **npx-aware MCP wiring** — the registered serve command survives the npx cache so
  Claude can still spawn it. (#142)

### Added
- **Jito hosted surface: read ops live.** `getTipFloor` and the status/account reads
  serve live against mainnet; the money-movers (`sendBundle`/`sendTransaction`) stay
  catalog-only (the agent submits those directly to Jito — we are the catalog, not the
  relay). (#144)

### Note
- **Realigns npm + PyPI.** 0.4.6 was an npm-only re-stamp: `pyproject.toml` was never
  bumped, so the binary reported `0.4.5` and PyPI never advanced past 0.4.5. This release
  moves every marker to 0.4.7 in lockstep, so `gecko --version` is honest again and both
  registries land on the same version.

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
