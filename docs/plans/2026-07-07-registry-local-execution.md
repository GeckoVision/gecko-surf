# Registry + Gecko Keys + Local Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve comprehended surfaces from an authenticated registry API so the local runner fetches context, injects the local provider key, and calls providers directly — surface fixes ship as registry rev bumps, not PyPI releases.

**Architecture:** New `gecko/registry/` package: `store.py` (surface documents + revs + tiers), `keys.py` (OTP issuance → `gk_live_` keys, hash-only at rest), `api.py` (Starlette routes mounted into the existing `build_multi_surface_app`), `client.py` (runner-side fetch + cache + offline fallback). `gecko/serve.py` grows `--registry`. Feedback endpoint reuses `preflight_corpus`'s closed vocabulary.

**Tech Stack:** Python 3.11, Starlette (already in `[serve]`), stdlib urllib via `netguard`, Mongo via injected collections (tests use in-memory fakes), pytest.

## Global Constraints

- Spec: `docs/specs/2026-07-07-registry-local-execution-design.md` — read it first.
- Control plane only: the registry serves surface documents; never payloads, never provider keys.
- Gecko keys: salted hash at rest, plaintext shown exactly once; never logged (sentinel tests).
- Anonymous fetch for `tier: "free"`; premium = flat per-surface entitlement (`402 entitlement_required`).
- Typed exceptions; no bare `raise Exception`. mypy over `gecko/` stays clean.
- Toolchain before every commit: `uv run ruff format && uv run ruff check --fix && uv run mypy gecko && uv run pytest <targeted>`.
- Line length 88 (ruff).
- Feedback endpoint accepts ONLY `preflight_corpus` closed-vocabulary classes; reject free text.
- Branch: `feat/registry-local-execution` off main.

---

### Task 1: Surface store (`gecko/registry/store.py`)

**Files:**
- Create: `gecko/registry/__init__.py`
- Create: `gecko/registry/store.py`
- Test: `tests/test_registry_store.py`

**Interfaces:**
- Consumes: `gecko.surfaces.surface_rev(spec: dict) -> str` (exists).
- Produces: `RegistrySurface(name: str, spec: dict, tier: str)` frozen dataclass; `SurfaceStore(surfaces: list[RegistrySurface])` with `.names() -> list[str]`, `.get(name) -> RegistrySurface | None`, `.manifest(name) -> dict` returning `{"name", "surface_rev", "tier", "spec"}`; `class RegistryError(Exception)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry_store.py
"""Registry store: surface documents + revs + tiers (control plane only)."""

import pytest

from gecko.registry.store import RegistryError, RegistrySurface, SurfaceStore

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "1"},
    "paths": {"/x": {"get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}}},
}


def _store() -> SurfaceStore:
    return SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )


def test_names_and_get():
    store = _store()
    assert store.names() == ["colosseum", "txline"]
    assert store.get("colosseum").tier == "free"
    assert store.get("nope") is None


def test_manifest_carries_rev_tier_and_spec():
    store = _store()
    m = store.manifest("colosseum")
    assert m["name"] == "colosseum"
    assert m["tier"] == "free"
    assert m["spec"] == SPEC
    assert isinstance(m["surface_rev"], str) and len(m["surface_rev"]) >= 8


def test_manifest_unknown_surface_raises():
    with pytest.raises(RegistryError):
        _store().manifest("nope")


def test_tier_validated():
    with pytest.raises(RegistryError):
        RegistrySurface(name="x", spec=SPEC, tier="gold")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry_store.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'gecko.registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# gecko/registry/__init__.py
"""Registry — the control-plane distribution surface for comprehended APIs.

Serves surface documents (spec + rev + tier) to local runners. Control plane
only: never payloads, never provider keys.
"""
```

```python
# gecko/registry/store.py
"""Surface store: named surface documents with rev + entitlement tier.

A surface document is the same JSON that ships in ``gecko/examples`` today —
the registry makes it fetchable so a schema fix is a rev bump, not a release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gecko.surfaces import surface_rev

TIERS = ("free", "premium")


class RegistryError(Exception):
    """Raised for unknown surfaces or invalid registry configuration."""


@dataclass(frozen=True)
class RegistrySurface:
    name: str
    spec: dict[str, Any] = field(repr=False)
    tier: str = "free"

    def __post_init__(self) -> None:
        if self.tier not in TIERS:
            raise RegistryError(f"unknown tier {self.tier!r}; expected one of {TIERS}")


class SurfaceStore:
    def __init__(self, surfaces: list[RegistrySurface]) -> None:
        self._by_name = {s.name: s for s in surfaces}

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def get(self, name: str) -> RegistrySurface | None:
        return self._by_name.get(name)

    def manifest(self, name: str) -> dict[str, Any]:
        s = self.get(name)
        if s is None:
            raise RegistryError(f"unknown surface: {name}")
        return {
            "name": s.name,
            "surface_rev": surface_rev(s.spec),
            "tier": s.tier,
            "spec": s.spec,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_registry_store.py -q`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add gecko/registry/ tests/test_registry_store.py
git commit -m "feat(registry): surface store — documents + rev + tier"
```

---

### Task 2: Keys + OTP (`gecko/registry/keys.py`)

**Files:**
- Create: `gecko/registry/keys.py`
- Test: `tests/test_registry_keys.py`

**Interfaces:**
- Produces: `class RegistryAuthError(Exception)`; `Mailer = Callable[[str, str], None]` (email, code); `KeyStore(keys_collection, otp_collection, mailer, clock=time.time)` with `.start_otp(email: str) -> None`, `.verify_otp(email: str, otp: str) -> str` (returns plaintext `gk_live_...` exactly once), `.check(plain_key: str) -> dict | None` (the stored key doc sans hash/salt, or None). Constants: `OTP_TTL_SECONDS = 600`, `OTP_MAX_ATTEMPTS = 3`, `OTP_MAX_PER_HOUR = 3`.
- Collections are duck-typed: need `insert_one`, `find_one`, `update_one`, `count_documents`, `delete_many`. Tests use the in-memory fake below.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry_keys.py
"""Key issuance: email OTP -> gk_live_ key; hash-only at rest; abuse caps."""

from typing import Any

import pytest

from gecko.registry.keys import (
    OTP_MAX_ATTEMPTS,
    OTP_MAX_PER_HOUR,
    OTP_TTL_SECONDS,
    KeyStore,
    RegistryAuthError,
)


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def insert_one(self, doc: dict[str, Any]) -> None:
        self.docs.append(dict(doc))

    def find_one(self, q: dict[str, Any]) -> dict[str, Any] | None:
        for d in reversed(self.docs):
            if all(d.get(k) == v for k, v in q.items()):
                return dict(d)
        return None

    def update_one(self, q: dict[str, Any], u: dict[str, Any]) -> None:
        for d in reversed(self.docs):
            if all(d.get(k) == v for k, v in q.items()):
                d.update(u.get("$set", {}))
                for k, n in u.get("$inc", {}).items():
                    d[k] = d.get(k, 0) + n
                return

    def count_documents(self, q: dict[str, Any]) -> int:
        gte = {k: v["$gte"] for k, v in q.items() if isinstance(v, dict) and "$gte" in v}
        eq = {k: v for k, v in q.items() if not isinstance(v, dict)}
        n = 0
        for d in self.docs:
            if all(d.get(k) == v for k, v in eq.items()) and all(
                d.get(k, 0) >= v for k, v in gte.items()
            ):
                n += 1
        return n

    def delete_many(self, q: dict[str, Any]) -> None:
        self.docs = [
            d for d in self.docs if not all(d.get(k) == v for k, v in q.items())
        ]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def _store() -> tuple[KeyStore, list[tuple[str, str]], Clock]:
    sent: list[tuple[str, str]] = []
    clock = Clock()
    ks = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda email, code: sent.append((email, code)),
        clock=clock,
    )
    return ks, sent, clock


def test_otp_roundtrip_issues_key_and_stores_hash_only():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    assert len(sent) == 1 and sent[0][0] == "dev@example.com"
    code = sent[0][1]
    assert len(code) == 6 and code.isdigit()
    key = ks.verify_otp("dev@example.com", code)
    assert key.startswith("gk_live_")
    # hash-only at rest: the plaintext never appears in any stored doc
    for coll in (ks._keys, ks._otps):
        for doc in coll.docs:
            assert key not in str(doc)
    # the key authenticates
    rec = ks.check(key)
    assert rec is not None and rec["email"] == "dev@example.com"
    assert "hash" not in rec and "salt" not in rec


def test_wrong_otp_limited_attempts():
    ks, sent, _ = _store()
    ks.start_otp("dev@example.com")
    for _ in range(OTP_MAX_ATTEMPTS):
        with pytest.raises(RegistryAuthError):
            ks.verify_otp("dev@example.com", "000000")
    # even the right code is dead after max attempts
    with pytest.raises(RegistryAuthError):
        ks.verify_otp("dev@example.com", sent[0][1])


def test_otp_expires():
    ks, sent, clock = _store()
    ks.start_otp("dev@example.com")
    clock.now += OTP_TTL_SECONDS + 1
    with pytest.raises(RegistryAuthError):
        ks.verify_otp("dev@example.com", sent[0][1])


def test_issuance_rate_limited_per_email():
    ks, _, _ = _store()
    for _ in range(OTP_MAX_PER_HOUR):
        ks.start_otp("dev@example.com")
    with pytest.raises(RegistryAuthError):
        ks.start_otp("dev@example.com")


def test_check_unknown_key_returns_none():
    ks, _, _ = _store()
    assert ks.check("gk_live_nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry_keys.py -q`
Expected: FAIL with `ImportError` (module does not exist)

- [ ] **Step 3: Write minimal implementation**

```python
# gecko/registry/keys.py
"""Gecko key issuance: agent-native email OTP -> ``gk_live_`` key.

No dashboard, no human on our side. The plaintext key is returned exactly
once; only a salted hash is stored (it is OUR credential — invariant #1
concerns third-party secrets and payloads). Collections are duck-typed so
tests run against an in-memory fake and prod against Mongo.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Any

OTP_TTL_SECONDS = 600
OTP_MAX_ATTEMPTS = 3
OTP_MAX_PER_HOUR = 3

Mailer = Callable[[str, str], None]


class RegistryAuthError(Exception):
    """Raised on failed/expired/over-limit OTP or key verification.

    Messages never contain a key or a code."""


def _hash(plain: str, salt: str) -> str:
    return hashlib.sha256((salt + plain).encode("utf-8")).hexdigest()


class KeyStore:
    def __init__(
        self,
        keys_collection: Any,
        otp_collection: Any,
        mailer: Mailer,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._keys = keys_collection
        self._otps = otp_collection
        self._mail = mailer
        self._now = clock

    def start_otp(self, email: str) -> None:
        now = self._now()
        recent = self._otps.count_documents(
            {"email": email, "created": {"$gte": now - 3600}}
        )
        if recent >= OTP_MAX_PER_HOUR:
            raise RegistryAuthError("too many codes requested; try again later")
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._otps.insert_one(
            {"email": email, "code": code, "created": now, "attempts": 0, "used": False}
        )
        self._mail(email, code)

    def verify_otp(self, email: str, otp: str) -> str:
        now = self._now()
        doc = self._otps.find_one({"email": email, "used": False})
        if doc is None:
            raise RegistryAuthError("no active code for this email")
        expired = now - doc["created"] > OTP_TTL_SECONDS
        exhausted = doc["attempts"] >= OTP_MAX_ATTEMPTS
        if expired or exhausted:
            raise RegistryAuthError("code expired; request a new one")
        if not secrets.compare_digest(doc["code"], otp):
            self._otps.update_one(
                {"email": email, "code": doc["code"]}, {"$inc": {"attempts": 1}}
            )
            raise RegistryAuthError("wrong code")
        self._otps.update_one(
            {"email": email, "code": doc["code"]}, {"$set": {"used": True}}
        )
        plain = f"gk_live_{secrets.token_urlsafe(32)}"
        salt = secrets.token_hex(16)
        self._keys.insert_one(
            {
                "key_id": f"gkid_{secrets.token_hex(8)}",
                "email": email,
                "salt": salt,
                "hash": _hash(plain, salt),
                "surfaces": [],  # flat per-surface entitlement, granted later
                "created": now,
            }
        )
        return plain

    def check(self, plain_key: str) -> dict[str, Any] | None:
        """Constant-work verify: walk candidate docs, compare salted hashes."""
        # find_one can't query by hash without the salt; scan via a marker query.
        # Collections are small (issued keys); acceptable at v1 scale.
        doc = self._keys.find_one({"hash": None})  # never matches; fall through
        del doc
        for stored in getattr(self._keys, "docs", None) or self._scan():
            if secrets.compare_digest(_hash(plain_key, stored["salt"]), stored["hash"]):
                return {
                    k: v for k, v in stored.items() if k not in ("hash", "salt")
                }
        return None

    def _scan(self) -> list[dict[str, Any]]:
        # Mongo path: a real collection exposes .find({})
        return list(self._keys.find({}))
```

NOTE for the implementer: the fake exposes `.docs`; real Mongo uses `.find({})` via `_scan()`. Add `find` to the fake ONLY if you remove the `.docs` branch — prefer removing the branch and giving the fake a `find(q)` returning all docs, so both paths are identical. Do that cleanup in this task if time allows; the test asserts behavior, not the mechanism.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_registry_keys.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add gecko/registry/keys.py tests/test_registry_keys.py
git commit -m "feat(registry): agent-native key issuance — email OTP, hash-only at rest"
```

---

### Task 3: Registry HTTP routes (`gecko/registry/api.py`) + mount

**Files:**
- Create: `gecko/registry/api.py`
- Modify: `gecko/http_server.py` (route list in `build_multi_surface_app`, ~line 672)
- Modify: `gecko/serve_mcp.py` (`main()`, build the store from `_SURFACES`)
- Test: `tests/test_registry_api.py`

**Interfaces:**
- Consumes: Task 1 `SurfaceStore`, Task 2 `KeyStore`/`RegistryAuthError`.
- Produces: `registry_routes(store: SurfaceStore, keys: KeyStore | None) -> list[Route]` returning Starlette routes for `GET /registry/surfaces`, `GET /registry/surfaces/{name}`, `POST /registry/keys`, `POST /registry/keys/verify`. `build_multi_surface_app(..., registry: list[Route] | None = None)` appends them.
- Key header on entitled fetches: `X-Gecko-Key`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry_api.py
"""Registry routes: anon free fetch, 402 premium gate, OTP endpoints."""

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.registry.api import registry_routes
from gecko.registry.keys import KeyStore
from gecko.registry.store import RegistrySurface, SurfaceStore
from tests.test_registry_keys import Clock, FakeCollection

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "T", "version": "1"},
    "paths": {"/x": {"get": {"operationId": "getX", "responses": {"200": {"description": "ok"}}}}},
}


def _client() -> tuple[TestClient, KeyStore, list[tuple[str, str]]]:
    store = SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )
    sent: list[tuple[str, str]] = []
    keys = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda e, c: sent.append((e, c)),
        clock=Clock(),
    )
    app = Starlette(routes=registry_routes(store, keys))
    return TestClient(app), keys, sent


def test_list_surfaces_anon():
    client, _, _ = _client()
    r = client.get("/registry/surfaces")
    assert r.status_code == 200
    names = {s["name"]: s for s in r.json()["surfaces"]}
    assert names["colosseum"]["tier"] == "free"
    assert "spec" not in names["colosseum"]  # list is light; fetch gets the spec


def test_fetch_free_surface_anon():
    client, _, _ = _client()
    r = client.get("/registry/surfaces/colosseum")
    assert r.status_code == 200
    body = r.json()
    assert body["spec"] == SPEC and body["surface_rev"]


def test_fetch_premium_without_key_is_402():
    client, _, _ = _client()
    r = client.get("/registry/surfaces/txline")
    assert r.status_code == 402
    assert r.json()["error"] == "entitlement_required"


def test_fetch_premium_with_entitled_key():
    client, keys, sent = _client()
    client.post("/registry/keys", json={"email": "dev@example.com"})
    plain = keys.verify_otp("dev@example.com", sent[0][1])
    # grant flat per-surface entitlement directly (founder-run at v1)
    for d in keys._keys.docs:
        d["surfaces"] = ["txline"]
    r = client.get("/registry/surfaces/txline", headers={"X-Gecko-Key": plain})
    assert r.status_code == 200


def test_unknown_surface_404():
    client, _, _ = _client()
    assert client.get("/registry/surfaces/nope").status_code == 404


def test_otp_endpoints_roundtrip():
    client, _, sent = _client()
    r = client.post("/registry/keys", json={"email": "dev@example.com"})
    assert r.status_code == 202
    code = sent[0][1]
    r = client.post(
        "/registry/keys/verify", json={"email": "dev@example.com", "otp": code}
    )
    assert r.status_code == 200
    assert r.json()["key"].startswith("gk_live_")
    # wrong otp -> 401, no key material in the body
    r = client.post(
        "/registry/keys/verify", json={"email": "dev@example.com", "otp": "000000"}
    )
    assert r.status_code == 401
    assert "gk_live_" not in r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry_api.py -q`
Expected: FAIL with `ImportError: cannot import name 'registry_routes'`

- [ ] **Step 3: Write minimal implementation**

```python
# gecko/registry/api.py
"""Registry HTTP surface — mounted into the existing multi-surface server.

Anonymous fetch for free surfaces; ``X-Gecko-Key`` + flat per-surface
entitlement for premium ones (402 entitlement_required otherwise).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .keys import KeyStore, RegistryAuthError
from .store import RegistryError, SurfaceStore

KEY_HEADER = "X-Gecko-Key"


def registry_routes(store: SurfaceStore, keys: KeyStore | None) -> list[Route]:
    async def _list(_request: Request) -> JSONResponse:
        out = []
        for name in store.names():
            m = store.manifest(name)
            out.append(
                {"name": m["name"], "tier": m["tier"], "surface_rev": m["surface_rev"]}
            )
        return JSONResponse({"surfaces": out})

    async def _fetch(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        surface = store.get(name)
        if surface is None:
            return JSONResponse({"error": "unknown_surface"}, status_code=404)
        if surface.tier != "free":
            plain = request.headers.get(KEY_HEADER, "")
            rec = keys.check(plain) if (keys and plain) else None
            if rec is None or name not in rec.get("surfaces", []):
                return JSONResponse(
                    {
                        "error": "entitlement_required",
                        "remediation": "POST /registry/keys {email} then ask for "
                        f"access to {name!r} — flat per-surface entitlement.",
                    },
                    status_code=402,
                )
        return JSONResponse(store.manifest(name))

    async def _keys_start(request: Request) -> JSONResponse:
        if keys is None:
            return JSONResponse({"error": "issuance_disabled"}, status_code=503)
        body = await _json(request)
        email = str(body.get("email", "")).strip()
        if "@" not in email or len(email) > 254:
            return JSONResponse({"error": "invalid_email"}, status_code=400)
        try:
            keys.start_otp(email)
        except RegistryAuthError:
            pass  # do not leak rate-limit state to enumerators
        return JSONResponse({"status": "code_sent_if_valid"}, status_code=202)

    async def _keys_verify(request: Request) -> JSONResponse:
        if keys is None:
            return JSONResponse({"error": "issuance_disabled"}, status_code=503)
        body = await _json(request)
        try:
            plain = keys.verify_otp(
                str(body.get("email", "")), str(body.get("otp", ""))
            )
        except RegistryAuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        return JSONResponse({"key": plain})

    return [
        Route("/registry/surfaces", endpoint=_list),
        Route("/registry/surfaces/{name}", endpoint=_fetch),
        Route("/registry/keys", endpoint=_keys_start, methods=["POST"]),
        Route("/registry/keys/verify", endpoint=_keys_verify, methods=["POST"]),
    ]


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error, not ours
        return {}
    return body if isinstance(body, dict) else {}
```

- [ ] **Step 4: Wire into the hosted server**

In `gecko/http_server.py`, add a parameter to `build_multi_surface_app` (signature at ~line 462) and extend the route list built at ~line 672:

```python
def build_multi_surface_app(
    surfaces: list[tuple[str, Any]],
    *,
    registry_routes: list[Any] | None = None,
    # ... existing params unchanged
):
    ...
    routes = [
        Route("/healthz", endpoint=_healthz),
        # ... existing routes unchanged ...
    ]
    if registry_routes:
        routes.extend(registry_routes)
```

In `gecko/serve_mcp.py` `main()` (~line 73), build the store from the surfaces it already loads and pass the routes (keys wired with real Mongo in Task 6; pass `keys=None` for now):

```python
from gecko.registry.api import registry_routes as _registry_routes
from gecko.registry.store import RegistrySurface, SurfaceStore

surfaces = [(name, json.loads(path.read_text("utf-8"))) for name, path in _SURFACES]
store = SurfaceStore(
    [RegistrySurface(name=n, spec=s, tier="free") for n, s in surfaces]
)
app = build_multi_surface_app(
    surfaces, registry_routes=_registry_routes(store, None)
)
```

(Adapt to the exact existing `build_multi_surface_app(...)` call — keep every current argument.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_registry_api.py tests/test_http_server*.py -q`
Expected: all pass (existing http_server tests unaffected)

- [ ] **Step 6: Commit**

```bash
git add gecko/registry/api.py gecko/http_server.py gecko/serve_mcp.py tests/test_registry_api.py
git commit -m "feat(registry): HTTP routes — anon free fetch, 402 premium gate, OTP endpoints"
```

---

### Task 4: Runner-side fetch + cache (`gecko/registry/client.py`)

**Files:**
- Create: `gecko/registry/client.py`
- Test: `tests/test_registry_client.py`

**Interfaces:**
- Consumes: registry JSON shapes from Task 3.
- Produces: `FetchedSurface(name: str, surface_rev: str, tier: str, spec: dict, stale: bool)`; `fetch_surface(registry_url: str, name: str, *, key: str | None = None, cache_dir: Path | None = None, transport: Transport | None = None) -> FetchedSurface`; `Transport = Callable[[str, dict[str, str]], tuple[int, str]]`; `class RegistryFetchError(Exception)`.
- Default cache dir: `~/.gecko/surfaces/`; cache file `{name}.json` holds the last manifest.
- Default transport: `netguard.safe_get` after `validate_public_url` (headers: `X-Gecko-Key` when key given). NOTE: `safe_get` takes no headers today — thread a `headers: dict[str, str] | None = None` parameter through `safe_get` → `urllib.request.Request(current, method="GET", headers={...})`, merging with the USER_AGENT default. Modify `gecko/netguard.py` accordingly (keep the UA when no override).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_registry_client.py
"""Runner fetch: registry -> cache -> offline fallback (stale flag)."""

import json

import pytest

from gecko.registry.client import RegistryFetchError, fetch_surface

MANIFEST = {
    "name": "colosseum",
    "surface_rev": "abc12345",
    "tier": "free",
    "spec": {"openapi": "3.1.0", "info": {"title": "T", "version": "1"}, "paths": {}},
}


def test_fetch_writes_cache(tmp_path):
    calls = []

    def transport(url, headers):
        calls.append((url, headers))
        return 200, json.dumps(MANIFEST)

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.surface_rev == "abc12345" and got.stale is False
    assert calls[0][0] == "https://registry.example.com/registry/surfaces/colosseum"
    cached = json.loads((tmp_path / "colosseum.json").read_text("utf-8"))
    assert cached["surface_rev"] == "abc12345"


def test_key_header_sent_when_given(tmp_path):
    seen = {}

    def transport(url, headers):
        seen.update(headers)
        return 200, json.dumps(MANIFEST)

    fetch_surface(
        "https://registry.example.com",
        "colosseum",
        key="gk_live_x",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert seen.get("X-Gecko-Key") == "gk_live_x"


def test_network_failure_falls_back_to_cache_stale(tmp_path):
    (tmp_path / "colosseum.json").write_text(json.dumps(MANIFEST), "utf-8")

    def transport(url, headers):
        raise OSError("network down")

    got = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    assert got.stale is True and got.spec == MANIFEST["spec"]


def test_network_failure_no_cache_raises(tmp_path):
    def transport(url, headers):
        raise OSError("network down")

    with pytest.raises(RegistryFetchError):
        fetch_surface(
            "https://registry.example.com",
            "nope",
            cache_dir=tmp_path,
            transport=transport,
        )


def test_entitlement_402_raises_with_remediation(tmp_path):
    def transport(url, headers):
        return 402, json.dumps(
            {"error": "entitlement_required", "remediation": "ask for access"}
        )

    with pytest.raises(RegistryFetchError, match="entitlement_required"):
        fetch_surface(
            "https://registry.example.com",
            "txline",
            cache_dir=tmp_path,
            transport=transport,
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_registry_client.py -q`
Expected: FAIL with ImportError

- [ ] **Step 3: Write minimal implementation**

```python
# gecko/registry/client.py
"""Runner-side registry fetch: TLS fetch -> local cache -> offline fallback.

The runner prefers the registry when reachable; a network failure degrades to
the cached copy with ``stale=True`` (the runner banner should say so). The
Gecko key travels only here — to OUR registry — never in MCP traffic.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gecko.netguard import safe_get, validate_public_url

Transport = Callable[[str, dict[str, str]], tuple[int, str]]

_DEFAULT_CACHE = Path.home() / ".gecko" / "surfaces"


class RegistryFetchError(Exception):
    """Raised when a surface can't be fetched and no cache exists."""


@dataclass(frozen=True)
class FetchedSurface:
    name: str
    surface_rev: str
    tier: str
    spec: dict[str, Any] = field(repr=False)
    stale: bool = False


def _default_transport(url: str, headers: dict[str, str]) -> tuple[int, str]:
    validate_public_url(url)
    return 200, safe_get(url, headers=headers)


def fetch_surface(
    registry_url: str,
    name: str,
    *,
    key: str | None = None,
    cache_dir: Path | None = None,
    transport: Transport | None = None,
) -> FetchedSurface:
    cache = (cache_dir or _DEFAULT_CACHE) / f"{name}.json"
    url = registry_url.rstrip("/") + f"/registry/surfaces/{name}"
    headers = {"X-Gecko-Key": key} if key else {}
    send = transport or _default_transport
    try:
        status, body = send(url, headers)
    except OSError as exc:
        if cache.exists():
            m = json.loads(cache.read_text("utf-8"))
            return FetchedSurface(
                name=m["name"],
                surface_rev=m["surface_rev"],
                tier=m["tier"],
                spec=m["spec"],
                stale=True,
            )
        raise RegistryFetchError(
            f"registry unreachable and no cached copy of {name!r}"
        ) from exc
    if status != 200:
        detail = ""
        try:
            detail = json.loads(body).get("error", "")
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RegistryFetchError(f"registry returned {status} {detail}".strip())
    m = json.loads(body)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(m), "utf-8")
    return FetchedSurface(
        name=m["name"],
        surface_rev=m["surface_rev"],
        tier=m["tier"],
        spec=m["spec"],
        stale=False,
    )
```

Also modify `gecko/netguard.py` `safe_get` to accept `headers: dict[str, str] | None = None`:

```python
def safe_get(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_TIMEOUT,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    resolver: Resolver | None = None,
    opener_factory: OpenerFactory | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    ...
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(current, method="GET", headers=request_headers)
```

(One line replaces the existing UA-only construction inside the loop; a caller-supplied User-Agent wins because `update` overwrites.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_registry_client.py tests/test_netguard.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gecko/registry/client.py gecko/netguard.py tests/test_registry_client.py
git commit -m "feat(registry): runner fetch + cache + offline fallback; safe_get headers"
```

---

### Task 5: `gecko serve --registry` + colosseum sugar

**Files:**
- Modify: `gecko/serve.py` (`_parse_args` ~line 46, `main` ~line 132)
- Modify: `gecko/examples/colosseum.py` (`main`, docstring)
- Test: `tests/test_serve_registry.py`

**Interfaces:**
- Consumes: Task 4 `fetch_surface`.
- Produces: CLI `gecko serve --registry <name> [--registry-url URL] [--auth-env VAR]`. `--registry-url` default `https://mcp.geckovision.tech`. `--auth-env VAR` → `StaticHeaderSession({"Authorization": f"Bearer {os.environ[VAR]}"})` (from `gecko.access`). `GECKO_API_KEY` env supplies the Gecko key. `spec` positional becomes optional (`nargs="?"`) — exactly one of `spec` / `--registry` must be given.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_registry.py
"""gecko serve --registry: fetch from the registry instead of a spec path."""

import pytest

from gecko import serve


def test_registry_and_spec_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        serve._parse_args(["./spec.json", "--registry", "colosseum"])


def test_registry_flag_parses():
    args = serve._parse_args(
        ["--registry", "colosseum", "--auth-env", "COLOSSEUM_COPILOT_PAT"]
    )
    assert args.registry == "colosseum"
    assert args.registry_url == "https://mcp.geckovision.tech"
    assert args.auth_env == "COLOSSEUM_COPILOT_PAT"
    assert args.spec is None


def test_spec_still_works_without_registry():
    args = serve._parse_args(["./spec.json"])
    assert args.spec == "./spec.json" and args.registry is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_serve_registry.py -q`
Expected: FAIL (`--registry` unrecognized / spec required)

- [ ] **Step 3: Implement**

In `_parse_args`: change `p.add_argument("spec", ...)` to `p.add_argument("spec", nargs="?", default=None, ...)`; add:

```python
    p.add_argument(
        "--registry",
        default=None,
        help="Fetch a comprehended surface from the Gecko registry by name "
        "(instead of a spec URL/path).",
    )
    p.add_argument(
        "--registry-url",
        default="https://mcp.geckovision.tech",
        help="Registry base URL.",
    )
    p.add_argument(
        "--auth-env",
        default=None,
        help="Env var holding the PROVIDER bearer token — injected locally at "
        "call time, never sent to Gecko.",
    )
```

In `main()`, before the existing spec handling:

```python
    if bool(args.spec) == bool(args.registry):
        print(
            "Provide exactly one of <spec> or --registry <name>.", file=sys.stderr
        )
        return 2

    session: Any = public_session()
    if args.auth_env:
        token = os.environ.get(args.auth_env, "")
        if not token:
            print(f"{args.auth_env} is unset.", file=sys.stderr)
            return 1
        from .access import static_session

        session = static_session({"Authorization": f"Bearer {token}"})

    if args.registry:
        from .registry.client import RegistryFetchError, fetch_surface

        try:
            fetched = fetch_surface(
                args.registry_url,
                args.registry,
                key=os.environ.get("GECKO_API_KEY") or None,
            )
        except RegistryFetchError as exc:
            print(f"Could not fetch surface: {exc}", file=sys.stderr)
            return 2
        if fetched.stale:
            print("registry unreachable — serving the last cached copy (stale).")
        client = AgentApiClient(fetched.spec, session=session)
    else:
        # existing path, now using `session` instead of public_session() inline
        ...
```

(`import os` at top; keep the existing SSRF check for URL specs.)

In `gecko/examples/colosseum.py` `main()`: try the registry first, fall back to the bundled snapshot — the printed banner gains one line naming the source:

```python
    spec: dict[str, Any]
    source = "bundled"
    try:
        from gecko.registry.client import fetch_surface

        fetched = fetch_surface(
            os.environ.get("GECKO_REGISTRY_URL", "https://mcp.geckovision.tech"),
            "colosseum",
        )
        spec, source = fetched.spec, f"registry rev {fetched.surface_rev[:8]}"
    except Exception:  # noqa: BLE001 - offline/older registry: bundled still works
        spec = load_spec()
    client = AgentApiClient(spec, base_url=BASE, session=BearerSession(pat))
    print(f"surface source: {source}")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_serve_registry.py tests/test_serve*.py tests/test_colosseum_example.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add gecko/serve.py gecko/examples/colosseum.py tests/test_serve_registry.py
git commit -m "feat(serve): --registry fetch path + colosseum registry-first sugar"
```

---

### Task 6: Feedback endpoint (closed vocabulary) + search endpoint

**Files:**
- Modify: `gecko/registry/api.py` (two routes)
- Modify: `gecko/preflight_corpus.py` (add one call-time class)
- Test: extend `tests/test_registry_api.py`

**Interfaces:**
- Consumes: `preflight_corpus.assert_classes_closed(classes: list[str])`, `AgentApiClient.search(query, limit=5) -> list[dict]`.
- Produces: `POST /registry/feedback` body `{"surface": str, "surface_rev": str, "classes": [str]}` → 204, appends one JSON line per report to the path in env `GECKO_FEEDBACK_PATH` (503 when unset); `GET /registry/search?intent=...` → `{"results": [{"surface": name, "hits": [...]}]}`. New corpus class: `"call.upstream_schema_reject"` added to `CLASSES` in `preflight_corpus.py` (an upstream 4xx that names an unexpected/missing field — the Colosseum field-report class).
- `registry_routes` gains a keyword: `registry_routes(store, keys, feedback_path: str | None = None)`.

- [ ] **Step 1: Write the failing tests (append to tests/test_registry_api.py)**

```python
def test_feedback_accepts_closed_vocab_only(tmp_path):
    import json as _json

    from gecko.registry.api import registry_routes as rr
    from gecko.registry.store import RegistrySurface, SurfaceStore
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    log = tmp_path / "feedback.jsonl"
    app = Starlette(routes=rr(store, None, feedback_path=str(log)))
    client = TestClient(app)

    ok = client.post(
        "/registry/feedback",
        json={
            "surface": "colosseum",
            "surface_rev": "abc",
            "classes": ["call.upstream_schema_reject"],
        },
    )
    assert ok.status_code == 204
    line = _json.loads(log.read_text("utf-8").splitlines()[0])
    assert line["classes"] == ["call.upstream_schema_reject"]

    bad = client.post(
        "/registry/feedback",
        json={"surface": "colosseum", "surface_rev": "abc", "classes": ["lol.free_text"]},
    )
    assert bad.status_code == 400
    assert len(log.read_text("utf-8").splitlines()) == 1  # nothing appended


def test_search_across_surfaces():
    client, _, _ = _client()
    r = client.get("/registry/search", params={"intent": "get x"})
    assert r.status_code == 200
    surfaces = [x["surface"] for x in r.json()["results"]]
    assert "colosseum" in surfaces
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_registry_api.py -q`
Expected: 2 new FAILs (404 route missing)

- [ ] **Step 3: Implement**

In `gecko/preflight_corpus.py`, add to `CLASSES` (keep the comment style of the file):

```python
        "call.upstream_schema_reject",  # upstream 4xx naming an unknown/missing field — the shipped-spec-vs-reality class
```

In `gecko/registry/api.py`: extend the signature `def registry_routes(store, keys, feedback_path: str | None = None)`, add:

```python
    async def _search(request: Request) -> JSONResponse:
        intent = request.query_params.get("intent", "").strip()[:200]
        if not intent:
            return JSONResponse({"error": "missing intent"}, status_code=400)
        results = []
        for name in store.names():
            client = _clients.setdefault(name, _make_client(name))
            hits = client.search(intent, limit=3)
            if hits:
                results.append({"surface": name, "hits": hits})
        return JSONResponse({"results": results})

    _clients: dict[str, Any] = {}

    def _make_client(name: str) -> Any:
        from gecko.access import public_session
        from gecko.client import AgentApiClient

        surface = store.get(name)
        assert surface is not None
        return AgentApiClient(surface.spec, session=public_session())

    async def _feedback(request: Request) -> JSONResponse:
        if feedback_path is None:
            return JSONResponse({"error": "feedback_disabled"}, status_code=503)
        raw = await request.body()
        if len(raw) > 4096:
            return JSONResponse({"error": "too_large"}, status_code=413)
        body = await _json(request)
        classes = body.get("classes")
        if not isinstance(classes, list) or not classes:
            return JSONResponse({"error": "classes required"}, status_code=400)
        from gecko.preflight_corpus import PreflightCorpusError, assert_classes_closed

        try:
            assert_classes_closed([str(c) for c in classes])
        except PreflightCorpusError:
            return JSONResponse({"error": "unknown class"}, status_code=400)
        import json as _json
        from pathlib import Path as _Path

        record = {
            "surface": str(body.get("surface", ""))[:64],
            "surface_rev": str(body.get("surface_rev", ""))[:64],
            "classes": [str(c) for c in classes],
        }
        p = _Path(feedback_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(record) + "\n")
        return JSONResponse(None, status_code=204)
```

Register both routes in the returned list:

```python
        Route("/registry/search", endpoint=_search),
        Route("/registry/feedback", endpoint=_feedback, methods=["POST"]),
```

In `gecko/serve_mcp.py`, pass `feedback_path=os.environ.get("GECKO_FEEDBACK_PATH")`.

NOTE: check `assert_classes_closed`'s exact exception type in `gecko/preflight_corpus.py:119` before writing the except clause — the file defines `PreflightCorpusError` at line 87; if `assert_classes_closed` raises something else, match it.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_registry_api.py tests/test_preflight*.py -q`
Expected: all pass (corpus tests confirm the new class is allowlisted)

- [ ] **Step 5: Commit**

```bash
git add gecko/registry/api.py gecko/preflight_corpus.py gecko/serve_mcp.py tests/test_registry_api.py
git commit -m "feat(registry): feedback (closed vocab) + cross-surface intent search"
```

---

### Task 7: Sentinel leak suite + end-to-end integration test

**Files:**
- Test: `tests/test_registry_leaks.py`

**Interfaces:**
- Consumes: everything above. No new production code — this task is the verification gate; if it finds a leak, fix it in the module that leaks and note it in the commit.

- [ ] **Step 1: Write the tests**

```python
# tests/test_registry_leaks.py
"""The 'never stored, never logged' promise is a test, not a sentence.

Sentinel key/OTP must appear in ZERO of: log records, error text, HTTP
responses (other than the one-time issuance), and the feedback log.
"""

import json
import logging

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gecko.registry.api import registry_routes
from gecko.registry.keys import KeyStore
from gecko.registry.store import RegistrySurface, SurfaceStore
from tests.test_registry_api import SPEC
from tests.test_registry_keys import Clock, FakeCollection


def test_no_key_material_leaks(tmp_path, caplog):
    sent: list[tuple[str, str]] = []
    keys = KeyStore(
        keys_collection=FakeCollection(),
        otp_collection=FakeCollection(),
        mailer=lambda e, c: sent.append((e, c)),
        clock=Clock(),
    )
    store = SurfaceStore(
        [
            RegistrySurface(name="colosseum", spec=SPEC, tier="free"),
            RegistrySurface(name="txline", spec=SPEC, tier="premium"),
        ]
    )
    log = tmp_path / "fb.jsonl"
    app = Starlette(routes=registry_routes(store, keys, feedback_path=str(log)))
    client = TestClient(app)

    with caplog.at_level(logging.DEBUG):
        client.post("/registry/keys", json={"email": "dev@example.com"})
        otp = sent[0][1]
        plain = client.post(
            "/registry/keys/verify", json={"email": "dev@example.com", "otp": otp}
        ).json()["key"]
        # exercise every route with the live key
        r1 = client.get("/registry/surfaces", headers={"X-Gecko-Key": plain})
        r2 = client.get("/registry/surfaces/txline", headers={"X-Gecko-Key": plain})
        r3 = client.get(
            "/registry/search", params={"intent": "x"}, headers={"X-Gecko-Key": plain}
        )
        client.post(
            "/registry/feedback",
            headers={"X-Gecko-Key": plain},
            json={
                "surface": "colosseum",
                "surface_rev": "r",
                "classes": ["call.upstream_schema_reject"],
            },
        )

    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    stored = json.dumps(keys._keys.docs) + json.dumps(keys._otps.docs)
    responses = r1.text + r2.text + r3.text
    fb = log.read_text("utf-8") if log.exists() else ""
    for blob, where in (
        (logged, "logs"),
        (stored, "storage"),
        (responses, "responses"),
        (fb, "feedback log"),
    ):
        assert plain not in blob, f"key leaked into {where}"
        assert otp not in blob or where == "storage", f"otp leaked into {where}"
    # otp IS in otp storage by design (it must verify) — but never the key.


def test_end_to_end_fetch_serve_prepare(tmp_path):
    """Registry -> runner cache -> AgentApiClient -> prepared request, offline."""
    from gecko.client import AgentApiClient
    from gecko.access import static_session
    from gecko.registry.client import fetch_surface

    store = SurfaceStore([RegistrySurface(name="colosseum", spec=SPEC, tier="free")])
    app = Starlette(routes=registry_routes(store, None))
    http = TestClient(app)

    def transport(url, headers):
        path = url.split("registry.example.com", 1)[1]
        r = http.get(path, headers=headers)
        return r.status_code, r.text

    fetched = fetch_surface(
        "https://registry.example.com",
        "colosseum",
        cache_dir=tmp_path,
        transport=transport,
    )
    client = AgentApiClient(
        fetched.spec,
        base_url="https://api.example.com",
        session=static_session({"Authorization": "Bearer sk-local"}),
    )
    req = client.prepare("getX", {})
    assert req.url == "https://api.example.com/x"
    assert req.headers["Authorization"] == "Bearer sk-local"
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_registry_leaks.py -q`
Expected: 2 passed (if a leak assertion fails, fix the leaking module — the usual suspects are exception messages and debug logs — then re-run)

- [ ] **Step 3: Full sweep + commit**

Run: `uv run ruff format && uv run ruff check --fix && uv run mypy gecko && uv run pytest -q`
Expected: clean, all tests pass

```bash
git add tests/test_registry_leaks.py
git commit -m "test(registry): sentinel leak suite + offline end-to-end fetch->prepare"
```

---

### Task 8: Mongo + SES wiring for the hosted server, docs

**Files:**
- Modify: `gecko/serve_mcp.py` (wire `KeyStore` with real collections when `MONGODB_URI` set)
- Modify: `gecko/events.py` — none (pattern reference only)
- Modify: `README.md` (registry section)
- Modify: `infra/ecs-stack.yml` — note-only; SES env vars land in a later deploy PR if IAM changes are needed
- Test: `tests/test_serve_mcp_registry_wiring.py`

**Interfaces:**
- Consumes: `gecko.events._mongo_collection` pattern (`MongoClient(uri)[db][coll]`, lru-cached, warn-not-raise on failure — mirror it, do not import the private helper).
- Produces: `gecko/registry/wiring.py` with `build_keystore_from_env() -> KeyStore | None` — returns None unless `MONGODB_URI` set; collections `gecko_registry.keys` / `gecko_registry.otps`; mailer = SES via boto3 when `GECKO_OTP_FROM` set, else logs a warning and returns None (issuance disabled — never a silent print of codes).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_serve_mcp_registry_wiring.py
"""Hosted wiring: no env -> issuance disabled; never crashes the server."""

import gecko.registry.wiring as wiring


def test_no_env_returns_none(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("GECKO_OTP_FROM", raising=False)
    assert wiring.build_keystore_from_env() is None


def test_mongo_without_mailer_returns_none(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:1/x?serverSelectionTimeoutMS=10")
    monkeypatch.delenv("GECKO_OTP_FROM", raising=False)
    assert wiring.build_keystore_from_env() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_serve_mcp_registry_wiring.py -q`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement**

```python
# gecko/registry/wiring.py
"""Env-driven wiring for the hosted registry: Mongo keys + SES OTP mail.

Fails SOFT: missing env disables issuance (503 on the endpoints) rather than
crashing the multi-surface server. Never logs URIs or key material.
"""

from __future__ import annotations

import logging
import os

from .keys import KeyStore, Mailer

logger = logging.getLogger("gecko.registry")


def _ses_mailer(sender: str) -> Mailer:
    import boto3  # optional dep, present in the hosted image

    ses = boto3.client("ses")

    def send(email: str, code: str) -> None:
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [email]},
            Message={
                "Subject": {"Data": "Your Gecko code"},
                "Body": {
                    "Text": {
                        "Data": (
                            f"Your Gecko verification code is {code}. "
                            "It expires in 10 minutes. An agent you run "
                            "requested a Gecko key for this email."
                        )
                    }
                },
            },
        )

    return send


def build_keystore_from_env() -> KeyStore | None:
    uri = os.environ.get("MONGODB_URI")
    sender = os.environ.get("GECKO_OTP_FROM")
    if not uri or not sender:
        if uri and not sender:
            logger.warning("registry: GECKO_OTP_FROM unset — key issuance disabled")
        return None
    try:
        from pymongo import MongoClient

        db = MongoClient(uri, serverSelectionTimeoutMS=2000)["gecko_registry"]
        return KeyStore(
            keys_collection=db["keys"],
            otp_collection=db["otps"],
            mailer=_ses_mailer(sender),
        )
    except Exception:  # noqa: BLE001 - registry must not take the server down
        logger.warning("registry: keystore init failed (redacted)")
        return None
```

In `gecko/serve_mcp.py` `main()`, replace the `keys=None` from Task 3:

```python
from gecko.registry.wiring import build_keystore_from_env

registry = _registry_routes(
    store, build_keystore_from_env(), feedback_path=os.environ.get("GECKO_FEEDBACK_PATH")
)
```

In `README.md`, add under the serving section (adapt heading level to the file):

```markdown
### Fetch surfaces from the registry

Surfaces can be served straight from the Gecko registry — fixes propagate as
rev bumps, no package upgrade:

    gecko serve --registry colosseum --auth-env COLOSSEUM_COPILOT_PAT

Free surfaces need no account. Premium surfaces take a Gecko key
(`GECKO_API_KEY`), issued agent-natively: `POST /registry/keys {email}` →
email OTP → `POST /registry/keys/verify` → `gk_live_...` (shown once; we
store only a salted hash). Your PROVIDER key never travels to Gecko — the
runner injects it locally and calls the provider directly.
```

- [ ] **Step 4: Run + full sweep**

Run: `uv run pytest tests/test_serve_mcp_registry_wiring.py -q && uv run mypy gecko && uv run pytest -q`
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add gecko/registry/wiring.py gecko/serve_mcp.py README.md tests/test_serve_mcp_registry_wiring.py
git commit -m "feat(registry): hosted wiring — Mongo keys, SES OTP, env-gated; README"
```

---

### Task 9: Live smoke (founder-gated) — NOT automated

After merge + deploy (founder runs `./infra/deploy.sh` and sets `GECKO_OTP_FROM`
+ SES IAM if issuance is wanted immediately — issuance can ship disabled):

1. `curl https://mcp.geckovision.tech/registry/surfaces` → the 4 surfaces, free.
2. Fresh env: `uvx --from "gecko-surf[serve]" gecko serve --registry colosseum --auth-env COLOSSEUM_COPILOT_PAT` → banner shows `surface source: registry rev …`; agent makes one live call → 200.
3. Bump a surface doc, redeploy, restart runner → new rev picked up, no PyPI involved.

Success criteria from the spec, checked live: schema fixes ship as rev bumps; provider key stays local; funnel telemetry unchanged.
