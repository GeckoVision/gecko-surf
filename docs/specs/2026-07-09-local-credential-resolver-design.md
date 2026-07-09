# Local credential resolver — design (2026-07-09)

## Problem

The registry + local-execution architecture (see
`2026-07-07-registry-local-execution-design.md`) put the user's **provider key**
where it belongs: on the user's machine, injected by the local runner, calling the
provider directly — Gecko never sees it. That spec deferred *how the key gets onto
the machine* to "env var, exactly today's Session seam." This spec closes that gap.

Today a live session sources its provider credential from either:

- an **env var** (`TXODDS_API_TOKEN`, `COLOSSEUM_COPILOT_PAT`), or
- a **plaintext file** on disk (`~/.gecko/txodds-session.json`, written by the
  subscribe flow / demo).

Both leak by construction: env vars surface in `ps -e`, `/proc/<pid>/environ`, shell
history (`export TXODDS_API_TOKEN=...`), CI logs, and child processes; the dotfile
leaks via `cat`, backups, an accidental `git add`, and any process that can read the
home directory. The founder named the exact gap:

> "We don't touch user keys, but we also don't **help** them set their keys — and
> plaintext env/dotfile is not the better call."

Gecko's invariant is *never touch/transit the key*. That is preserved here and is not
in question. What this spec adds is the missing half: **help the local user hold their
own key safely**, without ever routing it through Gecko.

**One distinction, stated once.** This spec is about the **user's provider key** — the
secret that authenticates to the *real upstream API* (TxODDS, Colosseum, Nora). It is
**not** the Gecko-issued key (`gk_live_...`) from the registry spec — that one is
Gecko's own credential for identity/entitlement/telemetry, stored as a salted hash,
and it authenticates to *our* registry, never to a provider. From here on, "the
credential" / "the key" means the provider key.

## Decision (proposed — founder go/no-go)

Introduce a **credential resolver** behind the existing `AuthSession` /
`auth_headers() -> dict[str, str]` seam. The resolver fetches the provider credential
**at call time** from a **backend chain** with explicit precedence and graceful
degradation:

1. **OS keychain** (macOS Keychain, Linux Secret Service, Windows Credential Manager)
   via the optional `keyring` library — **the dev default**. Encrypted at rest,
   unlocked by OS login, never plaintext on disk.
2. **External secret manager** via a **command hook** — `op read`, `vault kv get`,
   `pass`, `gcloud secrets versions access` — for teams that centralize secrets.
3. **Env var** — documented as the **CI-only / headless fallback**, not the dev
   default.

A set-once CLI (`gecko auth set|rm|list`) writes to the keychain (never a dotfile).
Where a provider offers a shorter-lived credential (OAuth/STS/scoped token), the
resolver mints/exchanges it **locally** and caches the **short-lived** token in the
keychain — never the long-lived secret, and never through Gecko.

The seam does not change shape: a resolver is *just another `AuthSession`*. Recorded
mode still uses `stub_session()` and never resolves a real credential — the
one-code-path rule holds.

Rejected / out of scope: writing the key to any Gecko-controlled file or service;
a Gecko-run secret store; transiting the key to mint tokens server-side. All violate
the never-transit guarantee.

## Architecture

```
LOCAL RUNNER (gecko serve, unchanged transport edge)
  agent calls a tool
    -> client builds PreparedRequest from surface metadata
    -> session.auth_headers()            <-- SEAM (unchanged signature)
         ResolvedSession.auth_headers():
           header_name = surface.auth_mapping.header      # e.g. "X-Api-Token"
           secret      = resolver.resolve(ref)            # AT CALL TIME
             ref = CredentialRef(api="txodds", account=None)
             chain: keyring -> command-hook -> env  (first hit wins)
           return { header_name: render(scheme, secret) }
    -> caller.build_request(...) injects headers, host-pins (exfil guard, unchanged)
    -> caller.execute(...) fires at the PROVIDER directly
  secret lives in process memory for the call; not persisted, not logged.

CONTROL PLANE (Gecko registry) — never on this path. Serves the auth MAPPING
  (which header, which scheme) as control-plane metadata; NEVER the value.
```

The resolver sits **entirely inside the runner process**. Gecko cloud sees the auth
*mapping* (control plane — "this surface authenticates with header `X-Api-Token`",
already served by the registry manifest) but never the *value*.

## The resolver interface

A resolver is a small, injectable seam — a callable that turns a **reference** into a
**secret string**, plus an `AuthSession` adapter that turns the secret into the header
dict the engine already consumes. Typed sketch (illustrative; follows
`.claude/rules/python.md` — typed signatures, module error, redact-before-raise):

```python
# gecko/credentials.py  (new; ~one purpose, well under 300 lines)
from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

class CredentialError(Exception):
    """Resolution failed. MUST NEVER contain the secret value — only the ref,
    the backend that failed, and a remediation hint."""

@dataclass(frozen=True)
class CredentialRef:
    """WHICH credential to fetch — never the credential itself.
    `api` namespaces the entry (one machine, many providers); `account` scopes a
    named identity (e.g. two Colosseum PATs). Both are safe to log."""
    api: str
    account: str | None = None
    def slot(self) -> str:               # keychain "service"/env-suffix key
        return self.api if self.account is None else f"{self.api}:{self.account}"

@runtime_checkable
class CredentialBackend(Protocol):
    name: str                            # "keyring" | "command" | "env" — for messaging
    def available(self) -> bool: ...     # e.g. Secret Service reachable?
    def get(self, ref: CredentialRef) -> str | None: ...   # None = miss, not error

@dataclass
class ChainResolver:
    """Ordered backends; first non-None hit wins. Degradation is a miss, not a
    crash — a headless box with no keyring simply falls through to the next backend."""
    backends: list[CredentialBackend]
    def resolve(self, ref: CredentialRef) -> str:
        tried: list[str] = []
        for b in self.backends:
            if not b.available():
                continue
            tried.append(b.name)
            hit = b.get(ref)
            if hit is not None:
                return hit
        raise CredentialError(
            f"no credential for {ref.slot()!r} (tried: {', '.join(tried) or 'none'}). "
            f"Set one with: gecko auth set {ref.api}"
        )   # NOTE: message names the ref + backends only — never a value.
```

### How it plugs into `Session.auth_headers()`

`auth_headers()` stays `() -> dict[str, str]`. We add **one new `AuthSession`
implementation** alongside the existing `Session` / `NoAuthSession` /
`StaticHeaderSession` in `gecko/access.py`, so nothing downstream changes — the
client, caller, and exfil guard already consume any object with `auth_headers()`.

```python
# gecko/access.py  (new adapter; existing Session/stub_session untouched)
@dataclass
class ResolvedSession:
    """Live session that resolves its secret AT CALL TIME from the resolver chain.
    One header, one scheme, driven by the surface's auth MAPPING (control plane).
    The value is fetched fresh per call and never stored on the instance."""
    ref: CredentialRef
    header_name: str                      # from surface.auth_mapping, e.g. "X-Api-Token"
    scheme: str = "raw"                    # "raw" | "bearer"  (how to render)
    resolver: ChainResolver = field(default_factory=default_resolver)

    def auth_headers(self) -> dict[str, str]:
        secret = self.resolver.resolve(self.ref)          # may raise CredentialError
        value = f"Bearer {secret}" if self.scheme == "bearer" else secret
        return {self.header_name: value}
```

Key properties, mapped to the invariants:

- **Seam unchanged (invariant #2, #4).** A resolver *is* an `AuthSession`. The engine
  (`ingest`/`catalog`/`tools`/`caller`) is untouched; adding a resolver-backed API is
  pure adapter + data.
- **Call-time only.** `resolve()` runs inside `auth_headers()`, which the caller calls
  per request. No secret is stored on `ResolvedSession`, so a session object is safe to
  hold, log its `repr`, or serialize its config (ref + header name + scheme are all
  non-secret).
- **Recorded/live one code path (invariant #3).** Recorded mode uses `stub_session()`
  exactly as today — it returns fixed non-secret headers and **never constructs a
  resolver**. Only live mode builds a `ResolvedSession`. The transport edge is the
  only difference, as required.

## Backend chain, precedence, degradation

Precedence (first non-`None` wins), highest to lowest:

| # | Backend | When it fires | Availability check |
|---|---|---|---|
| 1 | `KeyringBackend` | dev default; anything `gecko auth set` wrote | `keyring` importable **and** a working backend (`keyring.get_keyring()` is not the fail/null backend) |
| 2 | `CommandBackend` | user configured a fetch command for this `api` | a command is configured for the ref |
| 3 | `EnvBackend` | CI / headless; `GECKO_CRED_<API>` or a legacy name like `TXODDS_API_TOKEN` | the env var is set and non-empty |

Rationale for order: the keychain is the safest *and* the one a human set
deliberately, so it wins; a configured command hook is an explicit team choice and
beats an ambient env var; env is last because it is the leakiest and most likely to be
a stale ambient value.

**Explicit precedence override.** `GECKO_CRED_BACKEND=env` (or `keyring` / `command`)
pins a single backend and disables the chain — for CI that must be deterministic and
must *not* accidentally read a developer keychain, and for debugging "which backend am
I hitting?".

**Headless / degradation behavior.** On a headless Linux box with no Secret Service,
`KeyringBackend.available()` returns `False` and the chain falls through — **a miss,
never a crash**. Messaging is explicit and printed **once at runner start** (not per
call), and remediation is always actionable:

- keyring unavailable, env present:
  `auth: keyring unavailable (no Secret Service); using GECKO_CRED_TXODDS from env.`
- nothing resolves (raised as `CredentialError` on first call, caught by serve):
  `no credential for 'txodds'. On a desktop: gecko auth set txodds. In CI/headless:
  export GECKO_CRED_TXODDS=... (see docs/credentials).`
- keyring present but locked:
  surface the backend's own "keychain locked" error verbatim (it contains no secret),
  prefixed `auth: keychain is locked — unlock it and retry.`

`keyring` is an **optional dependency** (a `credentials` extra), so a plain install
stays dep-light (`pyyaml` only, per the project's dep discipline). Absent the extra,
`KeyringBackend.available()` returns `False` and the chain degrades to command/env —
so `pip install "gecko-surf"` still runs; `pip install "gecko-surf[credentials]"`
gets the keychain default.

```toml
# pyproject.toml [project.optional-dependencies]
credentials = ["keyring>=25"]
```

## CLI surface

A new thin `gecko auth` subcommand group in `gecko/cli.py` (dispatch only; all logic
in `gecko/credentials.py`, per the thin-transport rule). It writes **only** to the
keychain — never a dotfile, never env.

```
gecko auth set <api> [--account NAME] [--scheme raw|bearer]
    Prompts for the secret with a HIDDEN prompt (getpass — no echo, no argv, no
    history). Stores it in the OS keychain under service="gecko:<api>[:account]".
    Refuses if no keyring backend is available and prints the env/command fallback
    instead of writing plaintext anywhere.
    NEVER accepts the secret as an argument (that would hit shell history / ps).

gecko auth rm <api> [--account NAME]
    Deletes the keychain entry. Idempotent; prints whether one existed.

gecko auth list
    Lists stored credential NAMES only — "<api>[:account]  (keyring)" — and which
    backend WOULD resolve each configured api. NEVER prints a value, a length, or a
    prefix. Also shows env/command-hook refs it can see (by name), so a user can
    audit "where would my txodds key come from?".

gecko auth test <api>          # optional, phase 2
    Resolves the credential and reports ONLY which backend answered + that a non-empty
    value came back (redacted "resolved ✓ via keyring"). Never echoes the value.
```

Notes:
- `set` reads via `getpass.getpass()` — the value never appears in `argv`
  (`/proc/<pid>/cmdline`, `ps`), shell history, or terminal scrollback.
- `--scheme` and the header name are non-secret and stored as the surface's auth
  mapping (control plane); only the value goes to the keychain.

## Short-lived credential / local OAuth exchange

Policy already set: **transit only the shortest-lived credential the provider offers**
— and here, transit-to-the-provider (never to Gecko). When a provider supports
OAuth/STS/scoped tokens, prefer holding the **long-lived** secret in the keychain and
minting a **short-lived** token per session/expiry — with the mint running **locally**
against the *provider's own* token endpoint, never through Gecko.

```python
# gecko/credentials.py  (mint adapter — illustrative)
@dataclass
class MintingBackend:
    """Exchanges a long-lived secret (resolved from the chain) for a short-lived
    provider token via the PROVIDER's own token endpoint, then CACHES the short token
    in the keychain under a `<api>:token` slot with its expiry. On cache-miss/expiry it
    re-mints. The long-lived secret is used only to mint and is itself keychain-held."""
    name = "mint"
    token_url: str                          # provider endpoint, host-pinned
    long_lived: ChainResolver
    clock: Callable[[], float] = time.time
    skew_s: int = 60
    def get(self, ref: CredentialRef) -> str | None:
        cached = _read_token_cache(ref)     # keychain slot "<api>:token"
        if cached and cached.expires_at - self.skew_s > self.clock():
            return cached.token
        secret = self.long_lived.resolve(ref)          # long-lived, keychain
        token = _mint(self.token_url, secret)          # LOCAL call to PROVIDER
        _write_token_cache(ref, token)                 # keychain, short-lived only
        return token.token
```

Constraints:
- **Mint host-pinned.** `_mint` validates `token_url` with `validate_public_url`
  (SSRF guard) and pins it to the surface's declared auth host — same posture as the
  caller's exfil guard. The mint never talks to Gecko.
- **Cache the short token, not the long secret**, so what sits at rest (even encrypted)
  has the smallest blast radius the provider allows.
- **Redact-before-raise** applies to the mint path too: a failed token exchange raises
  `CredentialError` with the status + endpoint host only — never the request body,
  never the long-lived secret.
- TxODDS's own two-token flow (`establish_session` → JWT + apiToken) is already a local
  exchange of exactly this shape; this generalizes it. A future hosted Gecko-run OAuth
  mint would need a *separate* spec and TEE attestation (named V-next in the registry
  spec) — explicitly **not** this.

## Security analysis (threat model)

What an attacker who lands on the developer's machine (a malicious dependency, a leaked
backup, a shoulder-surf of history/CI logs, a co-tenant reading `/proc`) can get:

| Vector | env var | plaintext dotfile | **keychain (default)** | external manager |
|---|---|---|---|---|
| `ps -e`, `/proc/<pid>/environ` | **exposed** | n/a | closed (not in env) | closed |
| shell history (`export ...`) | **exposed** | n/a | closed (`getpass`, no argv) | closed |
| child-process env inheritance | **exposed** (all children) | n/a | closed | closed (fetched per call) |
| CI log / build output | often **exposed** | n/a | n/a (CI uses masked env) | masked via manager |
| `cat`/backup/`git add` of home | n/a | **exposed** (plaintext) | closed (encrypted store) | closed (nothing on disk) |
| offline disk theft (powered off) | n/a | **exposed** | closed (encrypted at rest, OS-login key) | closed |
| malicious in-process code at call time | reads env | reads file | reads resolved value | reads resolved value |

Reading of the threat model:

- **env var** — leaks to any sibling process, `ps`, `/proc`, history, and most CI log
  captures. Fine only where the platform *masks* it (CI secret store) and the box is
  ephemeral — hence "CI-only fallback."
- **plaintext dotfile** — the worst at-rest posture: survives reboots, lands in
  backups, one `git add .` from a public leak. This is the thing we are eliminating as
  a default.
- **keychain** — moves the secret to an OS-managed **encrypted** store keyed to the
  user's login; not in env, not in argv, not a readable file. Closes every at-rest and
  ambient-process vector above. Residual risk: **in-process code at call time** (any
  backend must hand the plaintext to the process to build the header) and a
  **root/unlocked-session** attacker. Those are irreducible for a local BYOK model and
  are the same residual the current design already carries.
- **external manager** — strongest for teams: nothing at rest locally; the value is
  fetched per call and the manager enforces its own audit/rotation/revocation. Residual
  is again in-process-at-call-time.

**Command-hook capture (no history/log leak).** The hook contract runs the configured
command with `subprocess.run([...], capture_output=True, text=True)` — an **argv list,
never `shell=True`** — so the secret arrives on the child's **stdout**, not through a
shell that could log it. The command *string* the user configured (e.g.
`op read op://vault/txodds/credential`) is a **reference**, not the secret, and is safe
to store in `~/.gecko/config.toml` and to print in `auth list`. Captured stdout is
`.strip()`ed, held in memory for the call, and never logged. A non-zero exit raises
`CredentialError` with the command *name* and exit code — never its stdout.

**The never-transit guarantee (control-plane invariant #1).** The resolver lives
wholly inside the runner. No backend, no mint, no cache ever contacts a Gecko host: the
keychain is OS-local, the command hook targets the user's own manager, the env is
local, and the OAuth mint is host-pinned to the *provider*. Gecko's registry serves
only the auth **mapping** (header name + scheme) as control-plane metadata — the value
never leaves the machine. This is the same "by architecture, not policy" property the
registry spec established, now extended to *where the value rests* as well as *where it
flows*.

**Redaction — where the seam plugs in.** We already redact: `caller.build_request`'s
exfil guard names *only the host*, never the auth value; the CallError/telemetry paths
already avoid header values. This spec adds one rule at the new seam:
`CredentialError` (and the mint error) is constructed from the **ref + backend name +
remediation** only. A single leak test (below) asserts a sentinel secret never appears
in any `CredentialError`, log line, `auth list` output, `repr(ResolvedSession)`, or
telemetry event — the same sentinel discipline the registry leak suite uses.

## How it composes with registry + local execution

The resolver is the **local-key-handling layer of the already-decided architecture** —
it slots into the handshake at exactly the point the registry spec waved at ("inject
the LOCAL provider key (env var, exactly today's Session seam)"):

- **Registry (control plane)** serves the `SurfaceManifest`, including the **auth
  mapping** — which header, which scheme — but never the value. That mapping is what
  `ResolvedSession` reads for `header_name` / `scheme`.
- **Local runner** builds the `CredentialRef` from the surface name (`api = surface`,
  optional `--account`), constructs a `ResolvedSession`, and serves MCP. Per call, the
  resolver chain produces the header; the caller's host-pinning exfil guard (unchanged)
  ensures the resolved secret is only ever injected toward the surface's anchored host.
- **Anonymous free surfaces** (colosseum) need no *Gecko* key; they still need the
  user's *provider* PAT — which is exactly what the resolver supplies, upgrading
  today's `COLOSSEUM_COPILOT_PAT` env read to the keychain default with the env path
  preserved as the CI fallback.
- **surface_rev / no-PyPI-release** property is untouched: the resolver is runner-side
  code, orthogonal to surface content.

Migration is additive and non-breaking: the env var keeps working (it is backend #3),
so no existing user is broken; `gecko auth set` is the new recommended path and the
banner nudges toward it when it detects a bare env var on a keychain-capable box.

## Testing (Pattern B — first deliverable is the free offline falsifier)

The **first** deliverable is a $0, no-network, no-real-secret test that can falsify
resolution + redaction, using an **injected fake backend** (the light-fake discipline
from `python.md`, not heavy mocking):

```python
class FakeBackend:                         # in-memory, deterministic
    name = "fake"
    def __init__(self, store, up=True): self._s, self._up = store, up
    def available(self): return self._up
    def get(self, ref): return self._s.get(ref.slot())
```

1. **Resolution + precedence.** `ChainResolver([FakeBackend(env), FakeBackend(kr)])` →
   assert first-hit-wins; keyring beats env; miss falls through; all-miss raises
   `CredentialError` with a remediation string.
2. **Degradation.** `available()=False` keychain fake → chain falls to env fake, no
   crash; assert the start-banner message text.
3. **Redaction / leak suite (the falsifier).** Seed a sentinel secret
   `"SENTINEL-DO-NOT-LEAK"`; assert it never appears in `str(CredentialError)`,
   `repr(ResolvedSession)`, `auth list` output, the mint-error text, or any emitted
   telemetry event. This is the security deliverable — green with zero real secrets and
   zero network.
4. **Seam identity.** A `ResolvedSession` backed by a fake resolver drives the existing
   `caller.build_request` and produces the right header dict — proving the engine seam
   is unchanged (recorded call, $0).
5. **`getpass` / no-argv.** `auth set` reads via a patched `getpass` sink; assert the
   secret is never in the parsed argv and never written to any file (assert on a temp
   `$HOME`).
6. **Command hook.** A fake `subprocess` runner returns a sentinel on stdout; assert
   `shell=False`, stdout stripped, non-zero exit → `CredentialError` with the exit code
   and **not** the stdout.
7. **Mint path.** A fake clock + fake provider token endpoint: cache miss mints, cache
   hit within TTL does not re-mint, expiry re-mints; the long-lived secret is used only
   to mint; assert the mint target is host-pinned and never a Gecko host.
8. **Live smoke (final, founder-run, never the debugger).** Real keychain on a desktop:
   `gecko auth set colosseum` → `gecko serve --registry colosseum` → first call correct
   against the provider, with the PAT sourced from the keychain and no env var set.

Recorded mode stays fully offline: it uses `stub_session()` and never touches the
resolver — the leak/resolution suites carry the security proof.

## Success criteria

- A developer runs `gecko auth set colosseum`, then `gecko serve --registry colosseum`
  with **no env var and no dotfile**, and the agent's first call is correct — the PAT
  came from the encrypted keychain.
- On a headless CI box with only `GECKO_CRED_COLOSSEUM` set, the same serve works via
  the env fallback, with an explicit "using env; keyring unavailable" banner.
- The leak suite proves a sentinel secret never appears in any error, log, telemetry
  event, `auth list`, or session `repr`.
- No engine file (`ingest`/`catalog`/`tools`/`caller`) changed; the only `access.py`
  change is one new `AuthSession` adapter — the seam held.
- The provider key still never touches a Gecko host — proven by the same
  by-architecture argument as the registry spec, now including at-rest.

## Open questions (founder to decide)

1. **Config file for command hooks + refs.** Command-hook backends and per-surface
   auth-mapping overrides need *somewhere* to live. Proposed: `~/.gecko/config.toml`
   holding **references only** (command strings, header names, account aliases) — never
   values. Is a config file acceptable given we are trying to move *away* from dotfiles
   (this one holds no secrets), or do we prefer env-only configuration of hooks to keep
   the home dir empty?
2. **Do we ship the short-lived/OAuth mint in v1, or defer to v2?** No current surface
   (TxODDS, Colosseum, Nora) uses OAuth/STS — they use static PATs/tokens. Speccing the
   mint now keeps the interface honest, but building it before a provider needs it is
   speculative. Ship the `MintingBackend` interface but no concrete mint until API #2
   demands it?
3. **`keyring` as an extra vs. a soft-required dependency.** Optional keeps installs
   dep-light and CI clean, but means the *default* (keychain) is absent unless the user
   installs `[credentials]` — a papercut that could push people to env by inertia.
   Alternative: make `keyring` a base dependency so the safe path is the zero-config
   path. Dep-weight vs. safe-by-default — founder call.

## Phased build plan (what ships first)

- **Phase 0 (spec):** this document.
- **Phase 1 (the falsifier — ships first):** `gecko/credentials.py` with
  `CredentialRef`, `CredentialError`, `CredentialBackend`, `ChainResolver`,
  `EnvBackend`, and the `FakeBackend`-driven resolution + **redaction/leak** suite. No
  keychain, no network, no CLI. This is the $0 offline proof (Pattern B).
- **Phase 2 (keychain default + CLI):** `KeyringBackend` (optional `[credentials]`
  extra) + `ResolvedSession` in `access.py` + `gecko auth set|rm|list`. Wire the
  colosseum example to prefer keychain, keep env as fallback, add the start banner.
- **Phase 3 (external managers):** `CommandBackend` + `~/.gecko/config.toml` refs +
  `gecko auth test`. Docs for `op`/`vault`/`pass`/`gcloud`.
- **Phase 4 (short-lived, on demand):** concrete `MintingBackend` for the first
  provider that offers OAuth/STS — not before.
- **Final:** founder-run live smoke on a real keychain against Colosseum.

## Out of scope (named)

- Any Gecko-hosted secret store, Gecko-run OAuth mint, or hosted BYOK passthrough
  (separate spec; TEE-gated, named V-next in the registry design).
- The Gecko-issued registry key (`gk_live_...`) — that is the registry spec's concern;
  this resolver never handles it.
- Secret **rotation/expiry policy** for long-lived provider keys (the provider owns
  that; we only cache short-lived derived tokens).
- Storing anything about the credential on the control plane beyond the non-secret auth
  *mapping* already served by the registry manifest.
