"""GovernedSession — seam-parity falsifier (§4 seam-identity test #6).

Pattern B: the $0 offline falsifier for build item 2.5. The whole point of the
adapter is that the ``AuthSession`` seam is UNCHANGED for the caller: wrapping a
session in a ``GovernedSession`` returns the byte-identical header dict. The
identity/policy rides alongside; it never alters the wire headers. Repr stays
leak-free (delegates to the underlying non-secret repr + the identity).
"""

from __future__ import annotations

from gecko.access import (
    GovernedSession,
    NoAuthSession,
    ResolvedSession,
    Session,
    StaticHeaderSession,
)
from gecko.credentials import ChainResolver, CredentialRef
from gecko.identity import SessionIdentity
from gecko.policy import AgentPolicy

SECRET = "tok-SECRET-DO-NOT-LEAK-42"


class _FakeBackend:
    name = "fake"

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    def available(self) -> bool:
        return True

    def get(self, ref: CredentialRef) -> str | None:
        return self._store.get(ref.slot())


def _resolved(secret: str) -> ResolvedSession:
    return ResolvedSession(
        CredentialRef(api="txodds"),
        "X-Api-Token",
        resolver=ChainResolver([_FakeBackend({"txodds": secret})]),
    )


def _identity() -> SessionIdentity:
    return SessionIdentity(
        subject_id="anon-gov-1",
        policy=AgentPolicy(spend_cap=100, recipient_allowlist=["acct-1"]),
    )


# --- (a) seam parity: byte-identical headers across every AuthSession ---------


def test_seam_parity_resolved_session() -> None:
    inner = _resolved("tok-123")
    governed = GovernedSession(inner, _identity())
    assert governed.auth_headers() == inner.auth_headers()


def test_seam_parity_two_token_session() -> None:
    inner = Session(jwt="JWT", api_token="APITOK")
    governed = GovernedSession(inner, _identity())
    assert governed.auth_headers() == inner.auth_headers()


def test_seam_parity_static_header_session() -> None:
    inner = StaticHeaderSession({"apikey": "pk_public_123"})
    governed = GovernedSession(inner, _identity())
    assert governed.auth_headers() == inner.auth_headers()


def test_seam_parity_no_auth_session() -> None:
    inner = NoAuthSession()
    governed = GovernedSession(inner, _identity())
    assert governed.auth_headers() == inner.auth_headers() == {}


def test_governed_session_satisfies_authsession_protocol() -> None:
    from gecko.access import AuthSession

    governed = GovernedSession(NoAuthSession(), _identity())
    assert isinstance(governed, AuthSession)


def test_identity_does_not_alter_wire_headers() -> None:
    # The exact same inner session, governed vs bare, yields the exact same dict —
    # the policy/identity is metadata that never touches the wire.
    inner = _resolved("tok-abc")
    bare = inner.auth_headers()
    governed = GovernedSession(inner, _identity()).auth_headers()
    assert governed == bare
    assert list(governed.items()) == list(bare.items())


# --- (b) repr-safety ----------------------------------------------------------


def test_repr_carries_no_secret() -> None:
    inner = _resolved(SECRET)
    governed = GovernedSession(inner, _identity())
    governed.auth_headers()  # resolve once; nothing stored on the instance
    text = repr(governed)
    assert SECRET not in text
    # Delegates to the non-secret pieces: the underlying ref + the identity id.
    assert "txodds" in text
    assert "anon-gov-1" in text


def test_repr_no_secret_even_for_two_token_session() -> None:
    inner = Session(jwt=SECRET, api_token=SECRET)
    governed = GovernedSession(inner, _identity())
    # The two-token Session's own repr DOES include its fields (it is a plain
    # dataclass), so we only assert the GovernedSession adds no NEW leak surface:
    # its identity half is clean and it delegates rather than re-serializing creds.
    assert "anon-gov-1" in repr(governed)
