"""ResolvedSession — the live AuthSession that resolves its secret at call time.

Proves raw vs bearer rendering, that no secret is stored on the instance (repr is
safe even when the resolver holds the value), and — the seam invariant — that a
ResolvedSession backed by a fake resolver drives the existing caller path and
yields the right header dict (recorded, $0, no network).
"""

from __future__ import annotations

from gecko.access import ResolvedSession
from gecko.caller import build_request
from gecko.credentials import ChainResolver, CredentialRef

SENTINEL = "SENTINEL-DO-NOT-LEAK"


class _FakeBackend:
    """Light in-memory backend — deterministic, no network."""

    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def _resolver(secret: str) -> ChainResolver:
    return ChainResolver([_FakeBackend({"txodds": secret})])


def test_raw_scheme_renders_bare_value() -> None:
    session = ResolvedSession(
        CredentialRef(api="txodds"), "X-Api-Token", resolver=_resolver("tok-123")
    )
    assert session.auth_headers() == {"X-Api-Token": "tok-123"}


def test_bearer_scheme_prefixes_bearer() -> None:
    session = ResolvedSession(
        CredentialRef(api="txodds"),
        "Authorization",
        scheme="bearer",
        resolver=_resolver("tok-123"),
    )
    assert session.auth_headers() == {"Authorization": "Bearer tok-123"}


def test_resolves_fresh_each_call() -> None:
    store = {"txodds": "first"}
    session = ResolvedSession(
        CredentialRef(api="txodds"),
        "X-Api-Token",
        resolver=ChainResolver([_FakeBackend(store)]),
    )
    assert session.auth_headers()["X-Api-Token"] == "first"
    store["txodds"] = "rotated"  # rotate the secret behind the resolver
    assert session.auth_headers()["X-Api-Token"] == "rotated"


def test_repr_never_exposes_secret_even_if_resolver_holds_it() -> None:
    session = ResolvedSession(
        CredentialRef(api="txodds"), "X-Api-Token", resolver=_resolver(SENTINEL)
    )
    session.auth_headers()  # resolve once; still nothing stored on the instance
    text = repr(session)
    assert SENTINEL not in text
    # the resolver (which DOES hold the value) is deliberately excluded from repr
    assert "resolver" not in text
    assert "txodds" in text  # the non-secret ref IS present


def test_seam_identity_drives_existing_caller_path() -> None:
    # A ResolvedSession is just an AuthSession: its header dict flows through the
    # unchanged caller, proving the engine seam did not move.
    tool = {"_invoke": {"method": "GET", "path": "/v1/ping", "param_locations": {}}}
    session = ResolvedSession(
        CredentialRef(api="txodds"), "X-Api-Token", resolver=_resolver("tok-123")
    )
    req = build_request(
        tool, {}, "https://api.example.com", auth=session.auth_headers()
    )
    assert req.headers["X-Api-Token"] == "tok-123"
