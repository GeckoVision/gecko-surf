"""Caller — turn a question-shaped tool + agent args into a correct HTTP request.

This is where "the agent doesn't write integration code" actually pays off: the
agent supplies meaningful inputs (fixtureId, asOf); the caller places each in the
right spot (path / query / header), injects the hidden auth headers, and builds
the request. It also *catches* the failure the agent can't see — a missing
required path param — instead of firing a malformed call.

Stdlib only (urllib) so it ports anywhere with no deps.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from .netguard import validate_public_url

_UNFILLED = re.compile(r"\{([^}]+)\}")


class CallError(ValueError):
    """Raised when an agent's args can't form a valid request (caught, not fired)."""


@dataclass
class PreparedRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any | None = None


def build_request(
    tool: dict[str, Any],
    args: dict[str, Any],
    base_url: str,
    auth: dict[str, str] | None = None,
) -> PreparedRequest:
    invoke = tool["_invoke"]
    locations: dict[str, str] = invoke.get("param_locations", {})
    url_path = invoke["path"]
    query: dict[str, Any] = {}
    headers: dict[str, str] = {}

    for name, value in args.items():
        if name == "body":
            continue
        loc = locations.get(name, "query")
        if loc == "path":
            url_path = url_path.replace("{" + name + "}", quote(str(value), safe=""))
        elif loc == "header":
            headers[name] = str(value)
        else:  # query (default)
            query[name] = value

    missing = _UNFILLED.findall(url_path)
    if missing:
        raise CallError(
            f"missing required path parameter(s): {', '.join(missing)} for {invoke['path']}"
        )

    if auth:
        headers.update(auth)

    url = base_url.rstrip("/") + url_path
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"

    return PreparedRequest(
        method=invoke["method"],
        url=url,
        headers=headers,
        json_body=args.get("body"),
    )


def execute(req: PreparedRequest, timeout: int = 30) -> tuple[int, Any]:
    """Live execution (stdlib). Returns (status_code, parsed_json_or_text).

    Used only in live mode; the demo/tests run in recorded mode and never hit
    the network.
    """
    # SSRF guard before any live request: the base URL came from the spec (untrusted).
    validate_public_url(req.url)
    data = None
    headers = dict(req.headers)
    if req.json_body is not None:
        data = json.dumps(req.json_body).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        req.url, data=data, headers=headers, method=req.method
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except json.JSONDecodeError:
            return resp.status, body
