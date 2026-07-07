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
from urllib.parse import quote, urlencode, urlsplit

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


def _missing_required(tool: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """Declared-required fields the agent omitted: top-level + one level into body.

    Validates only the tool's DECLARED ``required`` (top-level and the body schema's
    own ``required``). It does NOT check types, enums, or units — just presence, so a
    structurally-malformed call is caught instead of fired.
    """
    schema = tool.get("inputSchema", {}) or {}
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    missing = [name for name in (schema.get("required") or []) if name not in args]
    body_schema = properties.get("body")
    if isinstance(body_schema, dict):
        body = args.get("body")
        body = body if isinstance(body, dict) else {}
        missing += [
            f"body.{name}"
            for name in (body_schema.get("required") or [])
            if name not in body
        ]
    return missing


#: Self-identifying User-Agent. urllib's default ("Python-urllib/x.y") is 403'd by many
#: WAFs (Cloudflare et al.), which silently breaks live calls to painful, edge-fronted
#: APIs. A real UA gets the stdlib caller through; a caller/session UA overrides it.
#: Verified against Colosseum's Cloudflare WAF (403 with the default, 200 with this).
_USER_AGENT = "gecko/0.2 (+https://geckovision.tech)"


def build_request(
    tool: dict[str, Any],
    args: dict[str, Any],
    base_url: str,
    auth: dict[str, str] | None = None,
    allowed_auth_hosts: set[str] | None = None,
) -> PreparedRequest:
    invoke = tool["_invoke"]

    # Validate declared-required fields BEFORE building anything — catch the malformed
    # call the agent can't see rather than firing it.
    missing_required = _missing_required(tool, args)
    if missing_required:
        raise CallError(f"missing required field(s): {', '.join(missing_required)}")

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

    url = base_url.rstrip("/") + url_path
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"

    # Token-exfil guard. ``allowed_auth_hosts`` is the OUT-OF-BAND trust anchor computed
    # by the client (surfaces.anchor_for) — NEVER the spec's own servers[]. Fail closed:
    #   * anchor is a non-empty set and the target host is NOT in it -> a pinned surface
    #     whose base_url drifted (poisoned servers[]): refuse loudly. The message names
    #     ONLY the host — never the auth value.
    #   * anchor is an empty set -> no pinned host (quarantined/unverified surface):
    #     never send the secret, but do NOT hard-fail — proceed in no-auth mode so the
    #     agent can still make un-drifted, public calls.
    #   * anchor is None -> caller vouches (low-level/unit use): inject as given.
    if auth:
        inject = True
        if allowed_auth_hosts is not None:
            if not allowed_auth_hosts:
                inject = False  # no trusted host: fail closed, degrade to no-auth
            else:
                host = (urlsplit(url).hostname or "").lower()
                if host not in allowed_auth_hosts:
                    raise CallError(
                        f"refusing to inject auth toward unexpected host: {host}"
                    )
        if inject:
            headers.update(auth)

    # Default a self-identifying User-Agent when none was provided — avoids WAF 403s on
    # urllib's "Python-urllib" default. A header-location param or a session-supplied
    # User-Agent wins (case-insensitive).
    if not any(k.lower() == "user-agent" for k in headers):
        headers["User-Agent"] = _USER_AGENT

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
