# Spec A — Session lifecycle (the auth handshake, generalized)

*2026-07-10 · owner: web3-engineer + staff-engineer (touches `access.py` = the seam) ·
status: DESIGN, ready to build falsifier-first*

> **Framing (post-correction, see [[specificity-not-positioning]]).** This is **sessions**
> — plumbing, not "the access wall," not "governance." We sell it as a *moment*, never a
> capability:
> *"Your token expired at 3am. Gecko refreshed it. Your agent didn't notice."*
> Do not reposition the company on this; it's a reliability feature that we **instrument**
> (the silent-refresh count is intended as the next honest number — corrected-sequencing
> item #3). **Status (2026-07-19): NOT built.** No silent-refresh counter is emitted yet —
> the SurfEvent vocabulary carries no refresh event. Treat this as an aspiration, not a
> shipped metric, until the counter lands.

## 1. The moment we're closing

Today `access.py` can *establish* a TxODDS session (`start_guest → subscribe → activate →
apiToken`) and inject its two tokens. What it **cannot** do:

- **Refresh.** A JWT/access token expires. Today the session holds a token forever; the next
  call 401s and the agent is stuck. (`@rileybrown`, 593❤: *"integrations need
  re-authentication."* — the real Tier-B evidence, not the 85–90% quote.)
- **Detect a dead session.** A revoked/expired credential returns 401/403 mid-run; the agent
  gets the failure raw, says "done!", and did nothing.
- **Re-auth automatically.** No path from "creds rejected" back to "creds valid" without a
  human re-running the handshake.

The generalized lifecycle: **establish → (serve) → detect-stale → refresh/re-auth →
(serve)** — all **behind the existing `auth_headers()` seam**, so the agent and the engine
never see it. The session self-heals; the caller is unchanged.

## 2. Scope

**In:**
- Generalize the two-token establish out of TxODDS into an API-agnostic `SessionLifecycle`.
- **Proactive refresh** — refresh *before* returning headers when the token is within a
  leeway window of expiry (no wasted 401 round-trip when we can see `exp`).
- **Reactive self-heal** — a bounded (once) retry hook: a call that 401/403s invalidates the
  session, re-establishes, and retries the *same* request once with fresh headers.
- Handshake adapters: **static token** (have it), **OAuth2** (client-credentials +
  refresh-token grant), **JWT-with-exp**, the **two-token subscribe** (generalized), and
  **API-key exchange** (key → short-lived token).
- Instrumentation: a control-plane-clean **silent-refresh counter** (counts + classes, never
  token values) — the metric that becomes the pitch. **(Not yet built — no refresh event is
  emitted today; this stays a planned metric.)**

**Out (honest boundaries — state them, don't paper over):**
- **True 2FA / human-in-the-loop auth is NOT automatable** — a hard wall for everyone. We own
  the *machine* access + session lifecycle; when a human must approve, we surface a typed
  `auth_interaction_required` refusal and stop. We never automate a human's 2FA.
- **We never mint or hold the provider's long-lived secret.** Refresh tokens / client secrets
  resolve through the credential resolver (keyring→command→env) in the *local runner*; the
  control plane stores none of it (invariant #1). The short-lived access token lives in local
  runner RAM only, never persisted.
- On-chain subscribe stays **founder-run** (CLAUDE.md) — the lifecycle *consumes* a signed
  txSig, it never signs or broadcasts.

## 3. The seam design (invariant-preserving)

The whole lifecycle hides behind the unchanged `AuthSession` Protocol:

```python
class AuthSession(Protocol):
    def auth_headers(self) -> dict[str, str]: ...
```

Two additive touch-points, both behind the seam:

1. **Proactive, inside `auth_headers()`.** A live session that knows its `exp` refreshes when
   `now + leeway >= exp`, *then* returns headers. Static/stub/no-auth sessions are unchanged
   (no `exp` → never refresh). The caller sees only a valid header dict, always.

2. **Reactive, at the caller's transport edge.** `caller.py`/the client path gains a **single
   bounded self-heal hook**: on a `401/403` from the upstream, call
   `session.invalidate()` and retry the *identical* `PreparedRequest` **once** with
   re-resolved headers. A second failure propagates as a typed `AuthError` (no infinite
   loop). Recorded mode never triggers this (no network).

New shape (additive, does not alter `auth_headers()`):

```python
@runtime_checkable
class RefreshableSession(Protocol):        # optional capability, duck-typed
    def auth_headers(self) -> dict[str, str]: ...
    def invalidate(self) -> None: ...      # mark stale → next auth_headers re-establishes
    def expires_at(self) -> float | None:  # epoch secs, or None if unknown/static
        ...
```

A plain `AuthSession` without `invalidate`/`expires_at` behaves exactly as today (the hook
is a no-op for it) — **100% back-compat**, proven by the seam-identity tests.

## 4. Handshake adapters (data + one seam, per invariant #2)

Each auth style is a `SessionLifecycle` strategy that produces/refreshes tokens; all expose
the same `RefreshableSession`. TxODDS-specific logic reduces to one adapter, not engine code.

| Adapter | establish | refresh | dead-session signal |
|---|---|---|---|
| `StaticTokenLifecycle` | resolve once (existing `ResolvedSession`) | n/a (no `exp`) | 401 → typed error (can't self-heal a static key) |
| `OAuth2Lifecycle` | client-credentials / auth-code exchange | refresh-token grant | 401 → refresh; refresh fails → re-establish |
| `JwtExpLifecycle` | resolve/exchange, read `exp` | re-exchange near `exp` | 401 → invalidate → re-establish |
| `TwoTokenSubscribeLifecycle` | `start_guest→activate` (generalize existing) | re-guest+activate (subscribe txSig cached) | 401 → re-activate; sub lapsed → `payment_required` |
| `ApiKeyExchangeLifecycle` | key → short-lived token | re-exchange | 401 → re-exchange |

## 5. Control-plane + governance ties

- Composes **under** `GovernedSession` (Phase 2): a `GovernedSession` can wrap a refreshable
  inner; `auth_headers()` still returns the inner's bytes verbatim, now self-healing. Policy
  stays out-of-band.
- Refresh uses the **credential resolver** for the long-lived secret; the control plane never
  sees it. Silent-refresh telemetry records `{surface, adapter, event: refreshed|reauthed|
  dead, count}` — **no token, no payload, no arg value** (reuse the `CallOutcome`/closed-vocab
  discipline from `preflight_corpus.py`).

## 6. Build plan — Pattern B, falsifier-first (offline simulation IS the first deliverable)

1. **Falsifier first (offline, $0).** A `FakeTransport` that returns `401` on the first call
   with a stale token and `200` after a refresh. Assert: proactive path refreshes before
   `exp`; reactive path self-heals a 401 in exactly one retry; a static session does **not**
   loop; a second 401 raises a typed `AuthError`; recorded mode never hits the transport.
   This test must exist and fail before any lifecycle code.
2. **`RefreshableSession` + the proactive branch** in `auth_headers()` for `JwtExpLifecycle`
   (the common case), leeway-configurable. Seam-identity test: a non-refreshable session is
   byte-identical to today.
3. **The reactive self-heal hook** in the caller/client path — bounded once, recorded-mode
   inert, redact-before-raise on the terminal `AuthError`.
4. **Generalize `TwoTokenSubscribeLifecycle`** out of the current TxODDS functions (keep the
   TxODDS demo green — it becomes the first adapter instance).
5. **`OAuth2Lifecycle` + `ApiKeyExchangeLifecycle`** (the adapters that unlock non-TxODDS
   APIs — needed for e.g. Jupiter/Helius if they gate).
6. **Silent-refresh telemetry** (counts/classes only) + the `auth_interaction_required`
   typed refusal for the 2FA boundary.
7. **Live smoke LAST** (never the debugger): one real expiring-token API end-to-end.

## 7. Seam-identity tests (the engine contract held)

- A plain `AuthSession` (no `invalidate`/`expires_at`) flows through `auth_headers()` and the
  self-heal hook **byte-identically** to today — `test_access`, `test_client_mcp`,
  `test_resolved_session`, the Phase-2 `test_governed_session` all stay green.
- Recorded mode makes **zero** network calls through the lifecycle (stub never refreshes).
- No secret/token appears in any `repr`, log, telemetry row, or raised error.
- Self-heal is bounded: at most one retry per call; no unbounded re-auth loop under repeated
  401s.

## 8. The metric this earns (why we build it now)

After it ships, instrument it: **"how many token refreshes did Gecko handle silently this
week?"** That is a number we *own*, generated by our own traffic — the honest replacement for
the browser-agent 85–90% stat we (correctly) threw out. It's the pitch for *sessions*, and
it's evidence, not a tweet.
