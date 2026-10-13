"""The HTTP seam an x402 payer needs: a reply with its status AND its headers.

The engine's transport (``gecko.caller.execute``) returns ``(status, body)``; an x402
challenge rides in a response HEADER (``PAYMENT-REQUIRED``) and a 402 arrives as an
``HTTPError``, so a payer needs both. ``live_http`` is the real wire, SSRF-guarded;
tests inject a fake :data:`HttpSend`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .netguard import USER_AGENT, validate_public_url

__all__ = ["HttpReply", "HttpRequest", "HttpSend", "live_http"]


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    json_body: Any | None = None


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]
    body: Any


#: request -> reply, status and headers included (a 402's challenge is a header).
HttpSend = Callable[[HttpRequest], HttpReply]


def live_http(
    req: HttpRequest, timeout: int = 30
) -> HttpReply:  # pragma: no cover - wire
    """The real wire, SSRF-guarded. A 4xx/5xx is a reply, not an exception."""
    validate_public_url(req.url)
    data = None
    headers = {"User-Agent": USER_AGENT, **req.headers}
    if req.json_body is not None:
        data = json.dumps(req.json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        req.url, data=data, headers=headers, method=req.method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return HttpReply(resp.status, dict(resp.headers), _parse(resp.read()))
    except urllib.error.HTTPError as exc:
        return HttpReply(exc.code, dict(exc.headers or {}), _parse(exc.read()))


def _parse(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text
