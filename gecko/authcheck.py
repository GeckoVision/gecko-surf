"""Live credential check — "do these creds ACTUALLY authenticate?"

``gecko auth test <surface>`` proves the keychain *resolves a value*, but it
cannot tell an expired/revoked token from a live one — a resolvable-but-dead
credential still reports ``resolved ✓``. ``--live`` closes that exact gap: build
the surface's keychain session, make ONE safe auth-gated GET, and report the HTTP
status — the only thing that truly distinguishes a working credential from a dead
one. (Discovered by dogfooding: a stale TxODDS session resolved fine yet 401'd.)

Logic lives here (the package); ``cli._auth_test`` is the thin transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .caller import LiveTransport


@dataclass
class ProbeResult:
    """The outcome of a live auth probe. ``ok`` iff the API accepted the credential."""

    ok: bool
    status: int | None
    op: str | None
    detail: str


def pick_probe_op(client: Any) -> str | None:
    """The safest liveness probe: the first usable AUTH-GATED ``GET`` with no
    required params, so calling it with ``{}`` genuinely exercises the credential
    (a 401/403 then means the token — not a missing arg — was rejected)."""
    for tool in client.list_tools():
        if not tool.get("requires_auth"):
            continue
        if str((tool.get("_invoke") or {}).get("method", "")).upper() != "GET":
            continue
        required = (tool.get("inputSchema") or {}).get("required") or []
        if not required:
            return str(tool["name"])
    return None


def live_probe(
    spec: dict[str, Any],
    surface: str,
    *,
    base_url: str | None = None,
    op: str | None = None,
    live_transport: LiveTransport | None = None,
) -> ProbeResult:
    """Resolve the surface's keychain session, make one safe authenticated GET, and
    classify the result. Never raises for the expected failures (missing/rejected
    credential) — those are the whole point, returned as a ``ProbeResult``."""
    # Local imports: keep this module importable without pulling the serve stack until
    # a probe actually runs, and avoid the access<-client import cycle at module load.
    from .access import keychain_session
    from .client import AgentApiClient
    from .credentials import CredentialError

    if base_url is None:
        servers = spec.get("servers") or [{}]
        base_url = servers[0].get("url") or None

    session, _warning = keychain_session(spec, surface)
    try:
        # Construction resolves the session (for the visibility check) — an unsealed
        # multi-scheme keychain surfaces here as a CredentialError, not a traceback.
        client = AgentApiClient(
            spec, base_url=base_url, session=session, live_transport=live_transport
        )
        probe = op or pick_probe_op(client)
        if probe is None:
            return ProbeResult(
                False,
                None,
                None,
                "no auth-gated GET with zero required params to probe — "
                "pass --op <operationId>.",
            )
        result = client.call(probe, {}, mode="live")
    except CredentialError as exc:
        return ProbeResult(False, None, op, f"credential missing — {exc}")

    status = result.get("status")
    if isinstance(status, int) and 200 <= status < 300:
        return ProbeResult(
            True, status, probe, f"credential authenticates (HTTP {status})."
        )
    if status in (401, 403):
        return ProbeResult(
            False,
            status,
            probe,
            f"credential REJECTED (HTTP {status}) — the keychain resolves a value, "
            "but the API rejects it (expired / wrong / revoked). Re-seal a fresh one.",
        )
    return ProbeResult(
        False,
        status if isinstance(status, int) else None,
        probe,
        f"reached the API (HTTP {status}) but auth was not conclusively confirmed.",
    )


def bundled_probe_target(surface: str) -> tuple[dict[str, Any], str] | None:
    """``(spec, base_url)`` for a bundled example surface so ``gecko auth test
    <surface> --live`` needs no ``--spec`` for the surfaces we ship — TxODDS being
    the two-token case that motivated this. ``None`` for anything else (the caller
    then requires ``--spec``)."""
    if surface == "txline":
        from .examples import txline

        return txline.load_spec(), txline.BASE_URL
    return None
