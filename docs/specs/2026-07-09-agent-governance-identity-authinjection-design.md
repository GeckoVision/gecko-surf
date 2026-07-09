# Agent governance — identity + governed auth injection — design (2026-07-09)

## Problem

Static keys are the wrong primitive for agents. A human holds one long-lived API key
and uses it deliberately; an agent holds the same key and can be *steered* — by a
poisoned tool description, a prompt-injected doc, a malicious upstream response — into
using it for something the operator never authorized. The failure mode is not
theoretical: businesses and individuals are getting **wallets drained** and
**credentials exfiltrated** because the credential is a bearer token with no binding to
*who is acting, on whose behalf, within what limits*. a16z's Keycard framing names the
gap directly — the agent needs a **governed identity**, not a static key.

Gecko already sits at the exact control point where this can be enforced. It
**comprehends** the API (so it knows read vs. write vs. transfer per operation), it
**injects** the credential at call time behind the `auth_headers()` seam (so the agent
never holds it — invariant #4), and it **scores + gates** every call before it fires
(`risk.py` → `enforce.py`). What is missing is the connective tissue: an explicit notion
of **agent identity** that binds a session to an **entitlement** and a **policy**, so
that "which credential, at what scope, for which operation" is a *governed* decision
rather than an implicit "inject the one key we have."

This spec designs that layer. It builds directly on:

- `2026-07-09-local-credential-resolver-design.md` — *where the provider key lives and
  how it is resolved at call time* (keychain → command → env, never transits Gecko).
- `2026-07-07-registry-local-execution-design.md` — *identity to Gecko* (the
  `gk_live_...` key, hashed) and the local runner that injects the provider key and
  calls the provider directly.

It is a **design spec only**. No code changes, no transaction is signed or broadcast,
and the uncommitted credential/context-hub work in the tree is untouched.

## The one distinction, stated once

There are **two credentials** in play and this spec keeps them apart at every step:

| | **Agent identity (to Gecko)** | **Provider credential (to the real API)** |
|---|---|---|
| What | the `gk_live_...` key from the registry spec + a derived **session identity token** | the TxODDS/Colosseum/Nora key |
| Authenticates to | *our* registry (entitlement, telemetry, policy binding) | the *upstream* provider, directly |
| Who holds it | issued by Gecko, **stored as a salted hash** | the user; resolved locally by the resolver |
| Transits Gecko? | yes — it *is* our credential, to our control plane | **never** — by architecture |
| Governs | *which* provider credential this agent may use, at *what* scope | nothing; it is the thing being governed |

The headline mechanism is the **binding between them**: the agent's identity to Gecko
selects — and *scopes* — the provider credential the resolver injects. The agent proves
*who it is*; Gecko decides *what it may do* and injects *only* the credential that policy
permits, at the narrowest scope the provider allows. The agent never sees either the
provider key or the raw entitlement — only the tool result.

## Decision (proposed — founder go/no-go)

Introduce an **agent-governance layer** with three cooperating pieces, none of which
changes the `auth_headers() -> dict[str, str]` seam:

1. **Agent identity** — a **session identity token** minted locally by the runner,
   derived from the registry `gk_live_...` key, binding *this agent session* to an
   entitlement and a **policy snapshot**. Represented on the control plane as a **hash**
   only (invariant #1); the plaintext lives in the runner process for the session.
2. **Governed auth injection** — the existing resolver injects the provider credential
   at call time, but now **scoped to the identity + the specific operation**: identity +
   policy decide *which* credential and *what scope*; the resolver mints (Keycard-style)
   the **shortest-lived, task-scoped** token the provider offers; the caller host-pins it
   (unchanged exfil guard). Auth stays invisible to the agent.
3. **Governance policy** — an explicit `AgentPolicy` (allowed APIs/operations, per-op
   **risk tier**, **spend caps**, allowlists) **auto-derived from comprehension** where
   possible, operator-tunable at the threshold level, mapped onto the existing
   `risk.py`/`enforce.py` allow/step-up/block gate.

Two enforcement points, **split by latency** (this split is already the architecture —
this spec names the identity/policy that flows through both):

- **Off-chain gateway** (`<50ms`, *every* call): comprehend → risk → anti-poison →
  **enforce** → inject-auth. Blocks a malformed / exfil / over-scope / over-cap call
  **before it fires**.
- **On-chain Pinocchio firewall** (only the agent's on-chain actions, DEVNET today, in
  `gecko-programs`): the last-resort gate — even if a malicious transaction reaches the
  chain, the transfer-hook **reverts it before custody moves**, and `gecko-receipt`
  attests the verdict.

Rejected / out of scope: Gecko custodying funds (we never hold or move funds — the
issuer runs the hook, Gecko only writes the verdict); a Gecko-hosted OAuth mint or
hosted BYOK passthrough (TEE-gated, named V-next in the registry spec); storing any
provider credential or plaintext identity token on the control plane.

## Architecture

```
LOCAL RUNNER (gecko serve --registry <name> [--key gk_live_...])
  identity establishment (once per session, at boot):
    gk_live_...  --(local derive, HMAC over key + session nonce + policy_hash)-->
      SessionIdentity{ agent_id, on_behalf_of, entitlement_ref, policy, token_hash }
    control plane sees: token_hash only  (invariant #1)

  per agent tool-call:
    McpSurface.call_tool(name, args)
      -> score = assess_from_client(...)          risk.py  (comprehension-native)
      -> gate  = apply_gate(score, enforce_mode)  enforce.py  (allow | step_up | block)
           policy scope/tier/cap fold INTO the score's inputs (see below)
      -> if blocked: refusal_payload -> agent  (upstream NEVER called)
      -> else: session.auth_headers()            <-- SEAM (unchanged signature)
           ResolvedSession.auth_headers():
             cred_ref, scope = policy.credential_for(identity, operation)
             secret          = resolver.resolve(cred_ref)      keychain -> command -> env
             (Keycard-style)  token = mint_scoped(secret, scope, ttl)   LOCAL, provider mint
             return { header_name: render(scheme, token) }
      -> caller.build_request(..., allowed_auth_hosts=anchor)  host-pinned exfil guard
      -> fire at the PROVIDER directly; response -> agent
    secret/token live in process memory for the call; never persisted, never logged.

ON-CHAIN TIER (gecko-programs, DEVNET, Anchor + Pinocchio) — only agent on-chain actions
  gecko-firewall (Pinocchio Token-2022 transfer-hook, per-mint denylist the ISSUER runs)
    -> reverts a disallowed transfer BEFORE custody moves; Gecko never moves funds
  gecko-receipt (content-addressed verdict-hash PDA)  -> tamper-proof attestation
  Gecko writes ONLY the verdict-hash / denied-wallets; the hook is the enforcement.

CONTROL PLANE (Gecko registry) — serves policy + auth MAPPING (which header/scheme) +
  entitlement; stores identity-token HASH. NEVER a provider key, NEVER a payload.
```

The whole governance decision lives **inside the runner process**. Gecko cloud serves
the *policy* and the auth *mapping* (control plane) and stores the identity *hash* — it
never sees the provider credential, the minted token, or the response payload.

## 1. Agent identity — who, on whose behalf, in what session

Today Gecko has one identity primitive: the registry `gk_live_...` key (hashed at rest),
which answers "is this a paying/entitled caller." That is **coarse** — it identifies a
*key holder*, not an *agent session*. Governance needs the finer grain: *this run of
this agent, acting for this principal, under this policy*.

We derive a **session identity token** locally from the registry key at runner boot. It
is not a new secret to store — it is a *scoped, short-lived projection* of the key:

```python
# gecko/identity.py  (new; illustrative — one purpose, well under 300 lines)
@dataclass(frozen=True)
class SessionIdentity:
    """WHO is acting, ON WHOSE BEHALF, under WHAT policy — for one runner session.

    Derived locally from the registry key; the control plane only ever sees
    `token_hash`. Every field here is safe to log EXCEPT nothing secret lives here:
    the token itself is held separately and never placed on this dataclass."""
    agent_id: str            # stable per (key, session) — e.g. hash(key)[:12]:nonce
    on_behalf_of: str        # the principal the operator declares (org / user ref)
    entitlement_ref: str     # which registry entitlement this session may spend against
    policy: AgentPolicy      # the governance snapshot bound at bind-time (below)
    token_hash: str          # salted hash of the session token — the ONLY thing stored
```

Properties, mapped to invariants:

- **Bound at boot, immutable for the session.** The policy snapshot is captured when the
  session is established, so a mid-session registry change cannot silently *widen* a
  running agent's authority (it takes a restart — auditable). Narrowing (revocation) is
  handled separately (see revocation below).
- **Hash-only on the control plane (invariant #1).** The registry stores
  `token_hash` (salted), exactly as it already stores the `gk_live_...` hash. The
  plaintext session token exists only in the runner for the session and is never
  written to disk, logged, or placed in tool context.
- **Distinct from the provider credential.** `SessionIdentity` authorizes the agent *to
  Gecko*; it is the input to "which provider credential may this session use," never the
  provider credential itself.
- **Anonymous / free surfaces still work.** For a free surface (colosseum) there is no
  registry key; the runner mints a **local-only** anonymous identity (`agent_id` from a
  session nonce, `entitlement_ref="anon"`, `policy` = the free-tier default). Nothing is
  stored server-side. The provider PAT is still resolved locally — governance applies,
  entitlement does not.

Where it lives: entirely in the **local runner** (the registry-local-execution
architecture). Long-term storage of identity/session state is a `data-engineer` concern
(hash + entitlement in the registry's existing `gecko_keys` shape); this spec fixes only
the *shape* and the *never-store-plaintext* rule.

## 2. Governed auth injection — identity + operation → credential + scope

The resolver spec already injects the provider credential at call time, host-pinned,
hidden from the agent. Governance adds one decision **in front of** the resolve call:
*given this identity and this specific operation, which credential and what scope?*

```python
# gecko/access.py — ResolvedSession gains a governed variant (seam unchanged)
class GovernedSession:
    """A ResolvedSession whose credential ref + scope are chosen by policy per call.
    auth_headers() still returns dict[str,str]; the engine is untouched."""
    identity: SessionIdentity
    header_name: str; scheme: str           # from the surface auth MAPPING (control plane)
    resolver: ChainResolver

    def auth_headers_for(self, operation: Operation) -> dict[str, str]:
        cred_ref, scope = self.identity.policy.credential_for(operation)  # governance
        secret = self.resolver.resolve(cred_ref)         # keychain -> command -> env
        token  = self.resolver.mint_scoped(cred_ref, secret, scope)  # Keycard-style
        value  = f"Bearer {token}" if self.scheme == "bearer" else token
        return {self.header_name: value}
```

Note the seam still produces a header dict; the *only* new input is the operation, which
the caller already has. Key properties:

- **Scoped to identity + operation.** A read operation resolves a read-scoped credential;
  a write/transfer operation resolves a write-scoped one (or is blocked by policy before
  we ever resolve). The agent describes intent; Gecko chooses the credential — the agent
  never selects it.
- **Keycard-style short-lived mint (provider-dependent — stated honestly).** Where the
  provider supports OAuth/STS/scoped tokens, `mint_scoped` exchanges the long-lived
  keychain secret for a **task-scoped, short-TTL** token via the *provider's own* token
  endpoint (host-pinned, `validate_public_url`), caches only the short token, and
  **never hands a static key to the agent**. Where the provider offers *only* a static
  PAT (TxODDS, Colosseum, Nora today), `mint_scoped` is a pass-through — governance still
  applies at the gate, but the "identity-bound short-lived token" property is **as strong
  as the provider allows**. We do not fake a scope the provider cannot enforce; we
  document the degraded posture per surface.
- **Auth stays invisible (invariant #4).** Tool defs never expose the header, the scope,
  or the token. Injection happens at call time behind the seam.
- **Host-pinned (unchanged).** The minted token is injected only toward the surface's
  anchored host by `caller.build_request`'s existing exfil guard — a poisoned arg cannot
  route the credential off-host.
- **Recorded mode unchanged (invariant #3).** `stub_session()` resolves nothing and mints
  nothing; the one-code-path rule holds — governance is a live-mode concern only.

## 3. Governance policy — what the agent is allowed to do

The `AgentPolicy` is the operator-facing contract for authority. Its distinguishing edge
is **auto-derivation from comprehension**: because Gecko already parsed the surface, the
policy's *defaults* are computed, not hand-written. The operator tunes thresholds and
caps — they never author allow-rules from scratch (the "fast to configure" edge).

```python
@dataclass(frozen=True)
class AgentPolicy:
    allowed_surfaces: frozenset[str]         # which APIs this agent may call at all
    allowed_operations: frozenset[str]       # per-op allowlist (auto = the tool set)
    op_tier: Mapping[str, RiskTier]          # read < write < transfer/spend  (auto-derived)
    spend_caps: SpendCaps                     # per-op / per-session ceilings (operator-set)
    trusted_hosts: frozenset[str]            # the exfil anchor (auto = surface host)
    step_up_at: int = 30                     # thresholds — the only knobs an operator turns
    block_at: int = 60
```

**Auto-derivation (comprehension → policy):**

| Policy field | Auto-derived from | Operator's role |
|---|---|---|
| `allowed_operations` | the comprehended tool set (`policy_from_client`) | narrow it, never widen blind |
| `op_tier` | HTTP method + semantics: GET → read, POST/PUT/PATCH → write, transfer/spend semantics → **transfer** | confirm the transfer set |
| `trusted_hosts` | the surface anchor (`anchor.trusted_hosts`) | rarely touched |
| `step_up_at` / `block_at` | ships with the `risk.py` defaults (30/60) | tune per risk appetite |
| `spend_caps` | **not** auto — no safe default for "how much money" | **must** set for any spend-tier op |

**Risk tiers (read < write < transfer/spend)** are the governance vocabulary. They map
onto the existing scorer, not a parallel system:

- `read` — `risk.py`'s `_op_risk` adds 0; passes at low score.
- `write` — `op.write` (+15) / `op.destructive` (+30); a `step_up` band.
- `transfer/spend` — a **new tier**: any operation whose comprehension marks it as
  moving funds/custody. Policy sets these to **fail-closed** — resolve a scoped credential
  and honor the spend cap, or **block**. This is the tier the wallet-drain vector lives
  in, so it is the tier with the least benefit of the doubt.

**Spend caps** are enforced as an *additional blocking input* to the gate: a
transfer/spend op whose declared/estimated amount exceeds `spend_caps` produces a
categorical block (like `exfil.host` — see `BLOCKING_SIGNALS`), independent of the
additive score. Where the amount is not knowable off-chain (an opaque on-chain tx), the
cap is enforced **on-chain** by the firewall's denylist/limit, not off-chain — the two
enforcement points cover each other (section 4).

**Mapping to `enforce.py`:** the policy is the source of `allowed`, `trusted_hosts`,
`step_up_at`, `block_at`, and the (new) spend-cap and transfer-tier signals that feed
`score_call`. `apply_gate` is unchanged — it already turns a decided `block` into a
refusal and a `step_up` into an attached warning. Governance is **policy authoring +
two new blocking signals**, not a new gate.

## 4. Wallet-drain prevention (the headline)

The exact vector, walked end to end. A poisoned or steered agent is induced — by a
malicious tool description, an injected doc, or a manipulated upstream response — to
attempt a **custody-probe-pattern** action: a nested-CPI transfer that moves funds to an
**attacker-controlled recipient** (the adversarial program in `gecko-programs`'
`custody-probe`, DEVNET). Two independent gates must both fail for funds to move.

**Off-chain gateway (first, `<50ms`, every call — blocks before it fires):**

- **Scope** — if the operation is not in `allowed_operations`, `risk.py`'s
  `scope.not_allowed` (+45) fires; a transfer the agent was never authorized to make is
  refused before any credential is resolved.
- **Exfil host** — if the call tries to route a credential or funds toward a host outside
  `trusted_hosts`, `exfil.host` (a `BLOCKING_SIGNAL`) categorically blocks — the drain's
  "send it to the attacker" step never gets the credential.
- **Malformed / poisoned** — a nested-CPI/transfer call that is malformed for the API
  (`schema.required`/`schema.type`) or carries injection-shaped metadata
  (`poison.injection`, a `BLOCKING_SIGNAL`) is blocked as not-first-call-correct **and**
  not-safe.
- **Spend cap / transfer tier** — a spend-tier op over its cap is a categorical block; an
  unscored spend-tier write **fails closed** (`gate.unscored_write` — the existing
  fail-closed boundary in `enforce.py`).
- On block: `refusal_payload` → agent (it learns *why*), a `surf.blocked` event carries
  the **signal names only** (no arg values — control-plane-safe), and **the upstream call
  is never made**. The credential is never even resolved.

**On-chain Pinocchio firewall (last resort — reverts before custody moves):**

Even if a transaction somehow reaches the chain (an on-chain action the off-chain gate
could not fully judge, or a path outside the gateway), the `gecko-firewall` **Pinocchio
Token-2022 transfer-hook** — which **the issuer runs**, against a **per-mint denylist**
Gecko only *writes to* — **reverts a disallowed transfer before custody moves**.
`gecko-receipt` writes a content-addressed **verdict-hash PDA** as a tamper-proof
attestation of the decision.

**Precise custody boundary (non-negotiable):**

- **Gecko never holds or moves funds.** It has no custody at any point.
- **The issuer runs the hook.** The transfer-hook is the *provider's/issuer's* enforcement
  surface; Gecko's write is the *verdict* (the denied wallets / the verdict-hash), not a
  fund movement.
- **Claude never signs or broadcasts.** The on-chain subscribe/deploy is founder-run only
  (CLAUDE.md). Claude *simulates* (`sigVerify:false`, `replaceRecentBlockhash:true` — no
  spend) and hands the exact command over; a passing simulation is the deliverable. This
  spec neither signs nor broadcasts anything.

The two points are **complementary by latency**: off-chain catches the malformed / exfil
/ over-scope / over-cap call cheaply on *every* call; on-chain is the irreducible
backstop for the subset that is a real on-chain custody move, where "revert before funds
move" is the only guarantee that actually protects a wallet.

## 5. Honest maturity + control-plane

What is real vs. thesis — never overclaimed:

**SHIPPED (off-chain, in `surfcall`, tested):**
- The credential **resolver** (keychain → command → env), call-time injection, redaction
  leak-suite (`gecko/credentials.py`, `ResolvedSession`).
- The **enforce gate** — `risk.py` (comprehension-native scoring, categorical blocking
  signals, per-signal crash containment) + `enforce.py` (`apply_gate`, `GECKO_ENFORCE`,
  fail-closed on unscored writes) + `mcp_server.call_tool` scoring before the upstream
  call.
- **Anti-poisoning** (injection markers, secret-shaped args) and the **host-pinned exfil
  guard** in `caller.build_request`.

**DEVNET (separate repo `gecko-programs`, not mainnet):**
- `custody-probe` (the adversarial drain vector we test against), `gecko-firewall`
  (Pinocchio transfer-hook + per-mint denylist), `gecko-receipt` (verdict-hash PDA). Real
  code on DEVNET; **no mainnet claim** until the founder-run mainnet path is exercised.

**THESIS (designed here, not built):**
- The `SessionIdentity` token + policy binding (section 1).
- `GovernedSession` credential-selection by identity + operation (section 2).
- `AgentPolicy` with `op_tier` transfer tier + `spend_caps` as blocking signals
  (section 3).
- Keycard-style **short-lived, task-scoped mint** — and its honest caveat: it is only as
  strong as the provider's token endpoint allows; static-PAT providers get governance at
  the gate but not a scoped mint.

**Control-plane guarantees (invariant #1, restated for this layer):**
- **Never store payloads.** The gate scores inputs and blocks; it never inspects or
  persists response bodies (a deliberate GAP, per `enforce.py`).
- **Identity keys hashed.** The `gk_live_...` key and the session identity token are
  stored as salted hashes; plaintext lives only in the runner for the session.
- **The provider key never transits Gecko.** Resolved and injected locally; the minted
  token is minted against the *provider's* endpoint, host-pinned, never through a Gecko
  host — the same by-architecture argument as the resolver spec, now extended to the
  scoped-mint path.
- **Telemetry is signal-names only.** Blocked events carry code-constant signal names
  (`blocked_signals`), never arg values, hosts-as-values, or amounts.

### Threat model

| Vector | Off-chain gate | On-chain firewall | Residual |
|---|---|---|---|
| Steered agent calls an **unauthorized transfer op** | `scope.not_allowed` → block | denylist reverts | agent can retry within scope only |
| Poisoned metadata induces a **credential exfil** to attacker host | `exfil.host` categorical block; host-pin refuses injection | n/a (never reaches chain) | in-process code at call time (irreducible, local BYOK) |
| **Over-cap spend** (amount known off-chain) | spend-cap categorical block | limit enforced on-chain too | amount mis-estimation → falls to on-chain cap |
| **Opaque on-chain custody move** (amount not knowable off-chain) | fail-closed on unscored write | **firewall reverts before custody moves** | issuer must run the hook; DEVNET today |
| **Malformed / injection-shaped** call | `schema.*` / `poison.injection` block | n/a | narrow marker set (tuned to avoid false-blocking paying calls) |
| Steal the **session identity token** | hash-only at rest; short-lived; revocable | n/a | root/unlocked-session attacker (same residual as resolver spec) |
| MITM'd **policy** from registry | v-next manifest signing; TLS + quarantine today | n/a | v1 trusts TLS (named gap, registry spec) |

**Revocation.** Because the policy snapshot is bound at boot, a *widening* needs a
restart (auditable). A *revocation* (kill a compromised session) is enforced by the
registry marking the identity hash revoked; the runner re-checks entitlement on the
cadence the registry spec already defines, and the next resolve fails closed. Real-time
revocation of an in-flight session is a `staff-engineer` seam question, flagged not
solved.

### Phased roadmap (Pattern B — offline falsifier first)

- **Phase 0 (spec):** this document.
- **Phase 1 (the falsifier — ships first):** `AgentPolicy` + the transfer-tier and
  spend-cap **blocking signals** in `risk.py`, driven by an injected fake client and a
  fake operation set — a `$0`, no-network, no-secret suite that *falsifies* "a steered
  over-scope / over-cap transfer is blocked before it fires." This is the security
  deliverable, green offline (Pattern B).
- **Phase 2 (identity):** `gecko/identity.py` — `SessionIdentity`, local derivation from
  the registry key, hash-only control-plane shape, anonymous free-tier identity. Leak
  suite: the session token never appears in logs, telemetry, errors, `repr`, or MCP
  responses (sentinel discipline, as the resolver spec).
- **Phase 3 (governed injection):** `GovernedSession` selecting the credential ref +
  scope by identity + operation; `mint_scoped` pass-through for static-PAT providers.
  Seam-identity test proves the engine is unchanged.
- **Phase 4 (scoped mint, on demand):** a concrete Keycard-style mint for the first
  provider that offers OAuth/STS — not before (same discipline as the resolver's
  `MintingBackend`).
- **Phase 5 (on-chain binding):** the runner writes the off-chain verdict-hash to
  `gecko-receipt` and the denied-wallet set to `gecko-firewall` on DEVNET, closing the
  loop between the off-chain decision and the on-chain backstop. Founder-run;
  simulate-only from Claude.
- **Final (live smoke, founder-run, never the debugger):** a steered-agent red-team on a
  real surface — the off-chain gate blocks the drain; the DEVNET firewall reverts the
  residual on-chain probe; the receipt attests.

## Success criteria

- A steered agent attempting a `custody-probe`-pattern transfer to an attacker recipient
  is **blocked off-chain before the call fires**, with a readable refusal, in the `$0`
  offline falsifier — no network, no real secret, no chain.
- A spend-tier operation with no configured cap **fails closed**; one over its cap is
  categorically blocked; a read is unaffected.
- The session identity token is never stored in plaintext, never logged, never reaches
  the agent — proven by a sentinel leak suite.
- The provider credential still never touches a Gecko host (by-architecture argument
  intact); the scoped mint, where it exists, targets the provider's own endpoint only.
- No engine file (`ingest`/`catalog`/`tools`/`caller`) changed; `access.py` gains one
  governed adapter, the seam holds; `apply_gate` is unchanged.

## Open questions (founder to decide)

1. **Issue identity tokens now, or defer behind entitlement?** The `gk_live_...` key
   already gives coarse identity. A per-session `SessionIdentity` token unlocks scoped
   governance and revocation, but adds a mint + hash + revocation-cadence surface before a
   customer has asked for it. Ship the `SessionIdentity` *shape* + policy binding now
   (Phase 1–2) but keep the token derivation a no-op pass-through until the first
   governance customer demands per-session revocation?

2. **Spend caps: off-chain policy vs. on-chain enforcement — which is authoritative?**
   Off-chain caps are cheap and catch the amount-known case on every call; on-chain caps
   (firewall denylist/limit) are the only real guarantee for opaque custody moves but are
   DEVNET-only and require the *issuer* to run the hook. Do we present spend caps as an
   off-chain *policy* (honest: "we block the call we can see") with on-chain as the
   named backstop, or hold the "spend cap" claim until the on-chain path is exercised on
   mainnet (founder-run)?

3. **Keycard-style short-lived mint — accept the provider dependency, or wait for a
   provider that supports it?** No current surface (TxODDS, Colosseum, Nora) offers
   OAuth/STS scoped tokens — they are static PATs. We can ship the `mint_scoped`
   *interface* now (governance at the gate, pass-through mint) and be honest that the
   "identity-bound short-lived token" property is provider-gated, or defer the whole
   mint story until API #2 offers a real token endpoint. Speccing it now keeps the
   Keycard positioning honest; building it now is speculative.
