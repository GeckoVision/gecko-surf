"""SessionIdentity — leak suite + shape-now-token-later contract.

Pattern B: this is the $0 offline falsifier for build item 2.4. It proves the
identity binds a session to an ``AgentPolicy`` + a non-secret anon id, that no
secret/token ever appears in ``repr``/``str``, and that the pass-through token
seam is in place (no per-session minting today) without foreclosing a later
revocation phase.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from gecko.identity import IdentityError, SessionIdentity
from gecko.policy import AgentPolicy

SECRET = "sk-DONOTLEAK0123456789abcdefABCDEF"


def test_binds_policy_and_subject() -> None:
    policy = AgentPolicy(spend_cap=100, recipient_allowlist=["acct-1"])
    ident = SessionIdentity(subject_id="anon-abc123", policy=policy)
    assert ident.subject_id == "anon-abc123"
    assert ident.policy is policy


def test_default_policy_is_empty_agent_policy() -> None:
    ident = SessionIdentity(subject_id="anon-xyz")
    assert isinstance(ident.policy, AgentPolicy)
    assert ident.policy.spend_cap is None
    assert ident.policy.recipient_allowlist == frozenset()


def test_anonymous_factory_mints_non_secret_free_tier_id() -> None:
    a = SessionIdentity.anonymous()
    b = SessionIdentity.anonymous()
    # A stable, opaque, NON-secret free-tier id — distinct per identity.
    assert a.subject_id != b.subject_id
    assert a.subject_id.startswith("anon-")
    # It is an identifier, not a secret: safe to surface in repr.
    assert a.subject_id in repr(a)


def test_for_install_is_stable_across_calls() -> None:
    # The anonymous shape, made STABLE: bound to the persistent install id, so the SAME
    # install yields the SAME subject run after run (the anon-first funnel join).
    a = SessionIdentity.for_install("abc123")
    b = SessionIdentity.for_install("abc123")
    assert a.subject_id == b.subject_id == "anon-abc123"
    assert SessionIdentity.for_install("other").subject_id != a.subject_id
    assert a.subject_id in repr(a)  # non-secret identifier, safe to surface


def test_pass_through_token_is_none_today() -> None:
    # Shape-now-token-later: no per-session token is minted yet, so the underlying
    # session's own credentials are used unchanged. Callers already handle None.
    ident = SessionIdentity.anonymous()
    assert ident.bound_token() is None
    assert ident.is_token_bound() is False


# --- leak suite ---------------------------------------------------------------


def test_repr_carries_no_secret() -> None:
    # Even if an operator jams a secret-shaped policy value, repr must stay clean.
    ident = SessionIdentity(
        subject_id="anon-abc123",
        policy=AgentPolicy(spend_cap=Decimal("50"), recipient_allowlist=["acct-1"]),
    )
    text = repr(ident)
    assert SECRET not in text
    # Only non-secret identifiers + the policy SHAPE are surfaced.
    assert "anon-abc123" in text
    assert "SessionIdentity" in text


def test_str_carries_no_secret() -> None:
    ident = SessionIdentity.anonymous()
    assert SECRET not in str(ident)


def test_secret_shaped_subject_id_is_refused_without_echo() -> None:
    # Redact-before-raise: a secret-looking id is refused, and the rejected value
    # never appears in the raised message.
    with pytest.raises(IdentityError) as exc:
        SessionIdentity(subject_id=SECRET)
    assert SECRET not in str(exc.value)


def test_empty_subject_id_is_refused() -> None:
    with pytest.raises(IdentityError):
        SessionIdentity(subject_id="")


def test_no_bound_credential_on_instance() -> None:
    # A bound credential/token must never live as instance state (control-plane
    # invariant): scan every attribute value for the sentinel secret.
    ident = SessionIdentity.anonymous(
        policy=AgentPolicy(recipient_allowlist=["acct-1"])
    )
    for value in vars(ident).values():
        assert SECRET not in repr(value)
