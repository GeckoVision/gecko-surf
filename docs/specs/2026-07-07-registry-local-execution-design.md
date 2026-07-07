# Registry + Gecko keys + local execution — design (2026-07-07)

## Problem

Three problems, one architecture:

1. **BYOK on hosted surfaces.** Providers want customers on their own keys. Today the
   only BYOK path is local serve with the surface *bundled in the wheel* — so fixing a
   wrong schema (the 0.2.2 Colosseum corrections) requires a PyPI release and every
   user upgrading. Hosted serving would require the user's key to transit our infra.
2. **Feedback capture (the V2 tension).** The agent calls the API directly, so Gecko
   never sees call outcomes; the corpus has no consumer-side loop.
3. **Identity.** Telemetry's `client` label is self-declared and untrusted — we could
   not distinguish the co-founder from an external developer (2026-07-07 census).

## Decision (founder-approved 2026-07-07)

**Registry + local execution.** The control plane serves *context* — comprehended
surfaces (tool defs, instructions, schemas, auth *mapping*, `surface_rev`) — from an
authenticated registry API. The user's **local runner** downloads the surface, serves
MCP locally, builds each `PreparedRequest` from that metadata, injects the **local**
provider key, and calls the provider **directly**. Gecko never sees the provider key
and never sees the response payload.

Founder calls locked in:
- **Anonymous fetch for free surfaces** (colosseum); Gecko key required only where
  there is something to entitle. Friction kills the wedge.
- **Flat per-surface entitlement** when a surface is premium — never usage-metered,
  never take-rate (metering drags us toward being a rail; charter forbids).
- **Agent-native key issuance**: email OTP, no dashboard, no human on our side.
- **TEE/enclaves: V-next, named** — local execution already delivers "we can't see
  your key" for zero engineering cost; enclaves only earn their complexity if the
  hosted passthrough tier or a Gecko-run OAuth mint ever demands *verifiable*
  non-custody. No TEE claims in outward copy until one runs.

Rejected: hosted BYOK passthrough as the *primary* path (keys+payloads transit us —
kept as a named fallback tier, separate spec if demand shows); Gecko-billed master
key (= becoming the payment rail).

## Architecture

```
CONTROL PLANE (existing ECS host, mcp.geckovision.tech)
  GET  /registry/surfaces                 list surfaces visible to this key (or anon)
  GET  /registry/surfaces/{name}          SurfaceManifest: spec + tool defs +
                                          instructions + auth MAPPING + surface_rev
  GET  /registry/search?intent=...        cross-surface lexical capability search
                                          (the existing catalog, server-side)
  POST /registry/keys {email}             start OTP issuance (emails a 6-digit code)
  POST /registry/keys/verify {email,otp}  → gk_live_... (shown once; hash stored)
  POST /registry/feedback                 opt-in failure-class reports (see corpus)

LOCAL RUNNER (the existing gecko serve, one new flag)
  gecko serve --registry colosseum [--key gk_live_...]
    → fetch + cache surface (rev-pinned) → serve MCP locally
    → per call: PreparedRequest from metadata → inject LOCAL provider key
      (env var, exactly today's Session seam) → call provider directly
```

## Components

| Unit | Purpose | Notes |
|---|---|---|
| `gecko/registry/api.py` (new) | The registry routes, mounted on the existing multi-surface http server | Starlette routes next to the current ones; no new deploy unit |
| `gecko/registry/keys.py` (new) | Key issuance + verification. `gk_live_` + 32 random bytes; **salted hash stored** (our own credential — invariant #1 is about third-party secrets); OTP: 6 digits, 10-min TTL, 3 attempts, per-email rate limit | Mongo collections `gecko_keys`, `gecko_otps` (TTL index) |
| `gecko/registry/store.py` (new) | Surface storage + `surface_rev` versioning; entitlement check (`free` → anon OK; else key scope `surfaces:[...]`) | Surfaces are JSON documents — the same files that live in `gecko/examples/` today |
| `gecko/registry/client.py` (new) | Runner-side: fetch, verify shape, cache under `~/.gecko/surfaces/{name}@{rev}.json`, staleness check on start | Fetch over TLS; `validate_public_url` on the registry URL |
| `gecko/serve.py` | `--registry <name>` + `--key` (or `GECKO_API_KEY` env) | Bundled snapshot remains the offline fallback: prefer registry when reachable, warn when cache is stale |
| `gecko/examples/colosseum.py` | Becomes sugar for `gecko serve --registry colosseum`; keeps the bundled snapshot for offline/recorded | No behavior change for existing users |
| Corpus loop | Runner (opt-in flag `--report-failures`) POSTs **failure classes + surface_rev only**, using the existing `preflight_corpus` closed vocabulary + allowlist writer; signed by the Gecko key when present | No payloads, no arg values, no field values — same control-plane proof Preflight has |
| Email send | SES (already in AWS) for OTP mail | Template: code + "you asked an agent to create a Gecko key" |

## The handshake, end to end

1. `uvx --from "gecko-surf[serve]" gecko serve --registry colosseum`
2. Runner → `GET /registry/surfaces/colosseum` (anon — free surface). Gets manifest
   `rev=N`, caches it.
3. Runner boots MCP locally; banner shows surface name, rev, tool count, and where
   auth comes from (`COLOSSEUM_COPILOT_PAT`, local env).
4. Agent calls a tool → runner validates args against the schema (first-call-correct
   happens *here*), builds `PreparedRequest`, injects the local PAT (existing
   `Session.auth_headers()` + host-pinning exfil guard, unchanged), fires directly at
   `copilot.colosseum.com`. Response → agent. Gecko infra untouched.
5. (Opt-in) On a failure class match, runner posts `{surface: colosseum, rev: N,
   class: "schema.unrecognized_key"}` to `/registry/feedback`.
6. Surface fix lands → registry serves `rev=N+1` → every runner picks it up on next
   start. **No PyPI release.**

Premium surface variant: step 2 returns 402-style `entitlement_required` with the
key-issuance instructions; agent runs the OTP flow (or the human already has a key).
Flat per-surface — the entitlement is a set membership, not a meter.

## Security / invariants

- **Provider keys never transit Gecko** — by architecture, not policy.
- **Payloads never transit Gecko** — execution is local; the registry serves only
  surface documents (control plane).
- **Gecko keys**: hashes only at rest; the plaintext is shown once at issuance. Key
  appears in requests to *our* registry only — never in MCP traffic, never in tool
  context, never logged (redaction test with a sentinel key, same discipline as the
  passthrough design).
- **OTP abuse**: per-email and per-IP rate limits; codes single-use, 10-min TTL;
  issuance endpoint is the only unauthenticated POST.
- **Registry responses are signed content in v-next** (manifest signature so a MITM'd
  registry can't poison a surface); v1 relies on TLS + the existing quarantine
  posture (a fetched surface is still untrusted input to the runner's own checks).
- **Feedback endpoint accepts the closed vocabulary only** — reject free-text; cap
  body size; classes validated against `preflight_corpus`'s allowlist.

## Testing (Pattern B)

1. **`FixtureRegistry`** (in-process ASGI or local dir) drives all runner tests
   offline, $0: fetch → cache → serve → prepare → recorded call.
2. Key lifecycle: issue → verify → entitled fetch → revoked key → 401; OTP expiry,
   attempt cap, rate limit — all against a fake mail sink + fake clock.
3. Leak suite: sentinel Gecko key + sentinel OTP never appear in logs, telemetry
   events, error text, or MCP responses.
4. Corpus loop: a forced schema failure emits exactly the class + rev, nothing else
   (assert on the full posted body).
5. Registry-unreachable: runner falls back to bundled snapshot with a stale warning;
   fully offline recorded mode still green.
6. Live smoke (final, founder-run): fresh machine, no repo clone — uvx + anon fetch +
   real PAT → first call correct against Colosseum.

## Success criteria

- Colosseum served end-to-end from the registry: fresh env, zero files, zero PyPI
  involvement; a surface_rev bump propagates on runner restart.
- A schema fix like today's ships as a registry update, not a release.
- First Gecko key issued by an agent via OTP with no human on our side.
- Corpus receives its first consumer-side failure class (even from our own runners).

## Out of scope (V-next, named)

- Hosted BYOK passthrough tier (separate spec; short-lived-token transit policy).
- TEE/Nitro attestation for passthrough or a Gecko-run OAuth mint.
- Provider-side products: OAuth facade, agent-provisionable keys *for their APIs*.
- Self-serve key dashboard, billing rails, manifest signing, vector/GraphQL search
  (evidence-gated per the intent-layer spec; `/registry/search` stays lexical).
