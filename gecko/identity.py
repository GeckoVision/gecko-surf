"""SessionIdentity — bind a session to its operator policy (shape-now-token-later).

An identity answers *who is this agent and what governance applies to it* — it
binds a non-secret free-tier subject id to an ``AgentPolicy`` (the operator's
comprehension-derived governance intent). It is deliberately a **pass-through
shape today** (PRD decision #3): no per-session token is minted or revoked yet —
that is a later, customer-driven phase. The interface is built so that phase
slots in without a rewrite: ``bound_token()`` returns ``None`` now (use the
underlying session's own credentials unchanged) and returns a minted, revocable
token later; every caller already handles ``None``.

Control-plane invariant: an identity stores NO secret and NO token. Its ``repr``/
``str`` surface only non-secret identifiers + the policy shape. Any error is a
typed :class:`IdentityError`, and — redact-before-raise — a rejected value is
never echoed back in the message.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from .policy import AgentPolicy
from .sanitize import looks_like_secret_value

__all__ = ["IdentityError", "SessionIdentity"]

# A non-secret, opaque free-tier identifier prefix. The suffix is random but is
# an IDENTIFIER, not a credential — safe to log and surface in repr.
_ANON_PREFIX = "anon-"
_ANON_ENTROPY_BYTES = 8


class IdentityError(Exception):
    """Identity construction/binding failed.

    MUST NEVER contain a secret or token value — a rejected subject id is
    described by shape only, never echoed. The leak suite asserts this.
    """


@dataclass(frozen=True)
class SessionIdentity:
    """Binds a session to an ``AgentPolicy`` + a non-secret free-tier subject id.

    ``subject_id`` — an opaque, non-secret identifier for the agent/session (a
    free-tier anon id today). It is safe to log. ``policy`` — the operator's
    ``AgentPolicy`` (spend cap + recipient allow-list); an empty default is a
    no-op, so a plain identity governs nothing until an operator authors a policy.

    Shape-now-token-later: a per-session token would live behind ``bound_token()``
    (see the class docstring). It is intentionally NOT a stored field today, so an
    identity holds no credential state at all — nothing to leak.
    """

    subject_id: str
    policy: AgentPolicy = field(default_factory=AgentPolicy)

    def __post_init__(self) -> None:
        subject = self.subject_id
        if not isinstance(subject, str) or not subject.strip():
            raise IdentityError("subject_id must be a non-empty identifier")
        # Redact-before-raise: refuse a secret-shaped id, but NEVER echo the value.
        if looks_like_secret_value(subject):
            raise IdentityError(
                "subject_id looks like a secret; use a non-secret free-tier id"
            )

    @classmethod
    def anonymous(cls, policy: AgentPolicy | None = None) -> SessionIdentity:
        """Mint an identity with a fresh, opaque, non-secret free-tier subject id.

        The random suffix is an identifier (like a session id), never a credential,
        so it is safe to surface in ``repr`` and logs.
        """
        subject = _ANON_PREFIX + secrets.token_hex(_ANON_ENTROPY_BYTES)
        return cls(subject_id=subject, policy=policy or AgentPolicy())

    def bound_token(self) -> str | None:
        """The per-session identity token, or ``None`` when pass-through.

        Pass-through today (PRD decision #3): returns ``None``, meaning "no distinct
        session token — use the underlying session's own credentials unchanged." A
        later customer-driven revocation phase returns a minted, revocable token
        here; callers already branch on ``None``, so nothing above this changes.
        """
        return None

    def is_token_bound(self) -> bool:
        """Whether a per-session token is in force (always ``False`` while pass-through)."""
        return self.bound_token() is not None
