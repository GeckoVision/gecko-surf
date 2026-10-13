"""The allowed Orthogonal calls, built by the ordinary engine and sent by a transport.

Recorded and live share every line here; they differ only in the ``transport`` handed in
(a fixture, or ``live_transport``) and the session (none, or the keyring session).
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..access import AuthSession
from ..caller import PreparedRequest, build_request, execute
from ..credentials import CredentialError
from .errors import (
    KeyRejectedError,
    ListJobError,
    NoKeyError,
    NotAllowedError,
    OutOfCreditsError,
    UpstreamError,
)
from .extract import X402_RECEIPT_KEY
from .spec import (
    AUTH_HOSTS,
    BASE_URL,
    PINNED_PRICE_CENTS,
    RUN_OP,
    TARGETS,
    Target,
    check_allowed,
    target_for,
    tools_by_name,
)

#: request -> (status, parsed body). The seam recorded mode swaps.
Transport = Callable[[PreparedRequest], tuple[int, Any]]


class X402Caller(Protocol):
    """Pays for and makes one allowed ``/v1/run``-shaped call over x402 instead of the
    key (``gecko.anyquery.upstream_x402``). Returns the same envelope the key path
    returns: ``priceCents`` and ``data``, plus Gecko's own receipt."""

    def call(self, target: Target, body: dict[str, Any]) -> Any: ...


def live_transport(req: PreparedRequest) -> tuple[int, Any]:  # pragma: no cover - wire
    """The real wire: stdlib urllib via the engine's ``execute`` (SSRF guard included).

    A 4xx comes back as ``(status, body)`` rather than an exception, so the job can say
    what happened. Nothing from the request headers is ever put in an error.
    """
    try:
        return execute(req)
    except urllib.error.HTTPError as exc:
        try:
            body: Any = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            body = {}
        return exc.code, body
    except urllib.error.URLError:
        raise UpstreamError(
            "Could not reach api.orthogonal.com. Check the connection and retry."
        ) from None


@dataclass
class OrthogonalApi:
    transport: Transport
    session: AuthSession
    #: The allowlist this client enforces. Narrow it with ``dataclasses.replace``;
    #: never widen it from anything an agent or a model wrote.
    targets: tuple[Target, ...] = TARGETS
    #: When set (``GECKO_ORTHOGONAL_PAY=x402``), every ``/v1/run`` call is paid per call
    #: over x402 instead of with the key. The allowlist is checked first either way.
    x402: X402Caller | None = None
    _tools: dict[str, dict[str, Any]] = field(default_factory=tools_by_name, repr=False)

    def call(self, op: str, body: dict[str, Any] | None = None) -> Any:
        """Send one allowed call; return the parsed body of a 2xx, else raise typed."""
        check_allowed(op, body, self.targets)
        if self.x402 is not None and op == RUN_OP and body is not None:
            target = target_for(body.get("api"), body.get("path"), self.targets)
            if target is None:  # pragma: no cover - check_allowed refused it already
                raise NotAllowedError("that call is not on the allowlist")
            return self.x402.call(target, body)
        try:
            auth = self.session.auth_headers()
        except CredentialError:
            raise NoKeyError() from None
        args: dict[str, Any] = {"body": body} if body is not None else {}
        req = build_request(
            self._tools[op],
            args,
            BASE_URL,
            auth=auth,
            allowed_auth_hosts=set(AUTH_HOSTS),
        )
        try:
            status, payload = self.transport(req)
        except ListJobError:
            raise
        except Exception as exc:
            # The request may have reached Orthogonal before the wire broke, so this is
            # an upstream failure that may have been charged. Only the type is named:
            # a transport's message can carry a URL or a header.
            raise UpstreamError(
                f"The call to Orthogonal broke after it was sent "
                f"({type(exc).__name__}). It may have been charged."
            ) from None
        if status in (401, 403):
            raise KeyRejectedError(
                f"Orthogonal rejected the key (HTTP {status}). Nothing was spent. "
                "Set it again: `gecko auth set orthogonal --scheme bearer`.",
                status=status,
            )
        if status == 402:
            raise OutOfCreditsError("not enough", PINNED_PRICE_CENTS)
        if status >= 400:
            raise UpstreamError(
                f"Orthogonal answered HTTP {status} on {self._tools[op]['_invoke']['path']}.",
                status=status,
            )
        if isinstance(payload, dict) and X402_RECEIPT_KEY in payload:
            # Only Gecko writes an x402 receipt; one in a key-path reply is planted.
            payload = {k: v for k, v in payload.items() if k != X402_RECEIPT_KEY}
        return payload
