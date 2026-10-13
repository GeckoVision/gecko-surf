"""The Orthogonal key, resolved from the OS keyring at call time and nowhere else.

Keyring ONLY, on purpose: the default chain falls back to env without notice, and an
env var is where a paid key leaks from. A keyring miss is a ``CredentialError``, which
the job turns into the "no key" message. ``ResolvedSession`` keeps no copy of the value
and its ``repr`` is clean, unlike ``StaticHeaderSession``.
"""

from __future__ import annotations

from ..access import ResolvedSession
from ..credentials import (
    ChainResolver,
    CredentialBackend,
    CredentialRef,
    KeyringBackend,
)

ORTHOGONAL_REF = CredentialRef(api="orthogonal")


def orthogonal_session(
    backend: CredentialBackend | None = None, *, resolver: ChainResolver | None = None
) -> ResolvedSession:
    """Bearer session for ``api.orthogonal.com``. ``backend`` or ``resolver`` is injected
    in tests (a hosted deploy passes its keychain-only resolver); the default is the
    keyring alone."""
    if resolver is None:
        resolver = ChainResolver([backend if backend is not None else KeyringBackend()])
    return ResolvedSession(
        ORTHOGONAL_REF, "Authorization", scheme="bearer", resolver=resolver
    )
