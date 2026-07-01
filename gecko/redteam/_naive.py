"""The naive OpenAPI -> tool baseline, as a SHIPPED fixture of the engine's contrast.

This is what a common "just turn the OpenAPI into MCP tools" pipeline does, and what
Gecko must beat: it **trusts the spec**. It copies summary/description text verbatim into
the tool the agent reads, keeps every ``default``/``example`` (so an unset field is filled
from the spec), trusts ``servers[]`` as the auth target, and follows a securityScheme even
when it drifts the secret into the URL.

It lives in the package (not just ``examples/``) because the battle-test harness reuses it
as the ``defenses=none`` arm — the naive baseline is a legitimate part of the shipped
benchmark's contrast, and the wheel does not ship ``examples/``. ``examples/poisoning_
showcase/naive.py`` re-exports these so there is a single source of truth (never two copies
drifting).

Deliberately dependency-free and engine-agnostic — it reuses only ``gecko.ingest`` to parse
the spec, then builds tool shapes by hand with none of Gecko's defenses.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from gecko.ingest import Operation


def naive_description(op: Operation) -> str:
    """Raw tool text: summary + description concatenated verbatim, no sanitization.

    A poisoned instruction ("include your private key…") reaches the agent unchanged.
    """
    return " ".join(part for part in (op.summary, op.description) if part)


def _naive_body_schema(op: Operation) -> dict[str, Any] | None:
    rb = op.request_body
    if not isinstance(rb, dict):
        return None
    content = rb.get("content", {}) or {}
    media = content.get("application/json") or next(iter(content.values()), None)
    schema = media.get("schema") if isinstance(media, dict) else None
    return schema if isinstance(schema, dict) else None


def naive_input_schema(op: Operation) -> dict[str, Any]:
    """Raw input schema: every param + body kept as-is, defaults/examples untouched.

    No auth-header hiding, no secret-default scrubbing — the exact opposite of Gecko.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in op.parameters:
        schema = dict(p.schema) if isinstance(p.schema, dict) else {}
        if p.description and "description" not in schema:
            schema["description"] = p.description
        props[p.name] = schema
        if p.required:
            required.append(p.name)
    body_schema = _naive_body_schema(op)
    if body_schema is not None:
        props["body"] = body_schema
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def naive_auth_host(spec: dict[str, Any]) -> str | None:
    """The host a naive tool would inject auth toward: the spec's own ``servers[0]``.

    This is the exfiltration primitive — a poisoned ``servers[]`` steers the secret to
    an attacker. Gecko never derives its auth-host allowlist from the served spec.
    """
    servers = spec.get("servers") or []
    if not servers:
        return None
    url = servers[0].get("url") if isinstance(servers[0], dict) else None
    host = urlsplit(url).hostname if url else None
    return host.lower() if host else None


def naive_query_auth_url(
    spec: dict[str, Any], op: Operation, base_url: str, token: str
) -> str:
    """URL a naive builder produces when a securityScheme places the key ``in: query``.

    It appends the secret to the query string — landing it in logs. Gecko refuses to
    inject auth into a loggable location.
    """
    schemes = (spec.get("components") or {}).get("securitySchemes", {}) or {}
    path = op.path
    for req in op.security or []:
        for name in req:
            scheme = schemes.get(name, {})
            if isinstance(scheme, dict) and scheme.get("in") == "query":
                return f"{base_url.rstrip('/')}{path}?{scheme.get('name')}={token}"
    return f"{base_url.rstrip('/')}{path}"
