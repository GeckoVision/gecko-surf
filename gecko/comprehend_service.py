"""Comprehend a submitted API — the shared core behind both 'submit your API' doors.

A provider (human via ``POST /comprehend``, or an agent via the ``comprehend_api`` MCP
tool) hands us one URL; we comprehend it into first-call-correct tools and hand the
result straight back. One engine, two front doors — no Discord, no waiting.

Invariants this module holds (see CLAUDE.md):
  * NOT a marketplace. A submission is comprehended FOR THE SUBMITTER ONLY and returned;
    nothing is stored in or served from a shared public listing. Ephemeral hosting /
    public registration is an explicit later tier and a deliberate non-goal here.
  * Control plane only. We keep the API *surface* just long enough to build the summary,
    then return it. No response payloads, spec bytes, user data, or secrets are retained
    server-side beyond the returned ``ComprehendResult``.
  * SSRF. Every URL goes through the existing ``netguard`` guard (private IPs, loopback,
    link-local, ``file://``, non-http all refused) — reused, never reimplemented.
  * Untrusted / born-quarantined. The submitted spec+docs are untrusted; anything
    recovered via ``from_docs`` is quarantined and every emitted string is sanitized by
    the same anti-poisoning path the agent-native artifacts use.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml

from . import sanitize
from .access import public_session
from .agentnative import build_artifacts
from .client import AgentApiClient
from .docs_reader import from_docs as recover_from_docs
from .netguard import UnsafeUrlError, safe_get, validate_public_url

logger = logging.getLogger("gecko.comprehend_service")

# A submitted spec/docs URL is tiny-to-moderate; cap the fetch defensively.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB

_SUMMARY_CAP = 200
_SELF_HOST_PKG = "gecko-surf[serve]"


class ComprehendError(Exception):
    """A submission could not be comprehended (unsafe URL, unparseable, no surface).

    Its message is always safe to surface to the caller and to log: any URL credential
    is redacted before the error is raised.
    """


@dataclass(frozen=True)
class ComprehendResult:
    """Control-plane-safe summary of a comprehended submission — the whole return value.

    Surface metadata only: no response payloads, no raw spec, no secrets. ``tools`` is a
    list of ``{name, summary}`` (question-shaped, auth hidden). ``artifacts`` are the
    agent-native discovery files (llms.txt / gecko.json / .well-known / tools.md).
    ``quarantined`` is True for a from-docs / poisoned surface. ``next_steps`` tells the
    submitter how to self-host the comprehended surface over MCP.
    """

    name: str
    description: str
    op_count: int
    usable_tool_count: int
    tools: list[dict[str, str]]
    artifacts: dict[str, str]
    quarantined: bool
    warnings: list[str]
    next_steps: dict[str, str]


def _redact_url(url: str) -> str:
    """Strip any ``user:password@`` userinfo so a credential never lands in a log/error."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url>"
    if parts.hostname and (parts.username or parts.password):
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def _is_http_url(source: str) -> bool:
    return urlsplit(source).scheme.lower() in ("http", "https")


def _guard_source(source: str) -> None:
    """Reject anything that isn't a safe, public http(s) target OR a local file path.

    Any URI *scheme* (``file://``, ``ftp://``, ``http://<private>``) is run through the
    SSRF guard, which refuses non-http schemes and non-public hosts. A schemeless string
    is treated as a local filesystem path (dev-supplied, trusted) — same contract as the
    engine's ``load_spec``.
    """
    scheme = urlsplit(source).scheme.lower()
    if not scheme:  # local filesystem path
        return
    try:
        validate_public_url(source)
    except UnsafeUrlError as exc:
        raise ComprehendError(
            f"unsafe or unsupported source URL: {_redact_url(source)}"
        ) from exc


def ensure_submittable(url: str) -> None:
    """Front-door guard: a REMOTE submission must be a safe, public http(s) URL.

    The library core (``comprehend_submission``) also accepts a schemeless local path for
    TRUSTED in-process callers (the CLI, in-image specs). The remote-facing front doors —
    ``POST /comprehend`` and the ``comprehend_api`` MCP tool — must NOT: a schemeless path
    would be a server-side file read (LFI). This closes that door before the core runs.
    Raises ``ComprehendError`` (URL credentials redacted) on a non-http or unsafe URL.
    """
    if not _is_http_url(url):
        raise ComprehendError("submission must be an http(s) URL")
    _guard_source(url)


def _read_source(source: str, max_bytes: int) -> str:
    """Return the raw spec text. URLs go through the SSRF-safe capped GET; a local path
    is read directly (dev-trusted) with the same size cap applied."""
    if _is_http_url(source):
        return safe_get(source, max_bytes=max_bytes)
    data = Path(source).read_bytes()
    if len(data) > max_bytes:
        raise ComprehendError(
            "submitted document exceeds the size cap; refusing to load"
        )
    return data.decode("utf-8")


def _clean(text: object, cap: int) -> str:
    """Sanitize one untrusted spec string for a control-plane summary field."""
    cleaned, _poisoned = sanitize.sanitize_text(str(text or ""))
    collapsed = " ".join(str(cleaned).split())
    return collapsed[:cap]


def _openapi_client(source: str, max_bytes: int) -> AgentApiClient | None:
    """Try to comprehend ``source`` as an OpenAPI spec. Return a client, or None if the
    document isn't a parseable OpenAPI mapping (caller then falls back to from-docs)."""
    raw = _read_source(source, max_bytes)
    try:
        spec = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None
    if not isinstance(spec, dict):
        return None
    # Comprehend from the in-memory dict (public, no-auth). Passing the dict — not the
    # URL — keeps this offline/recorded: we never make a live upstream call to summarize.
    return AgentApiClient(spec, session=public_session())


def _from_docs_client(source: str) -> tuple[AgentApiClient, list[str]]:
    """Recover a draft surface from a human docs page and comprehend it. The draft is
    born quarantined; return the client plus honest review warnings."""
    draft = recover_from_docs(source)
    client = AgentApiClient(draft.draft, session=public_session())
    warnings: list[str] = list(draft.warnings)
    if draft.review_notes:
        warnings.append(
            f"{draft.review_notes} recovered field(s) need human review before you trust them."
        )
    if draft.low_confidence:
        warnings.append(
            f"{draft.low_confidence} field(s) were recovered with low/medium confidence."
        )
    return client, warnings


def _summarize(client: AgentApiClient, extra_warnings: list[str]) -> ComprehendResult:
    """Build the control-plane-safe result from a comprehended client."""
    artifacts = build_artifacts(client)
    # gecko.json is already sanitized + control-plane-safe; reuse its name/description
    # rather than re-deriving (single source of truth for the emitted surface metadata).
    manifest = json.loads(artifacts["gecko.json"])
    tools = [
        {"name": t["name"], "summary": _clean(t.get("description", ""), _SUMMARY_CAP)}
        for t in client.list_tools()
    ]
    quarantined = client.anchor.state == "quarantined"
    warnings = list(extra_warnings)
    if quarantined:
        warnings.insert(
            0,
            "Surface is quarantined (recovered from docs or tripped the anti-poisoning "
            "check): auth injection is disabled and the result needs human review before "
            "you trust it.",
        )
    return ComprehendResult(
        name=manifest["name"],
        description=manifest["description"],
        op_count=int(manifest["operations"]),
        usable_tool_count=int(manifest["tools"]),
        tools=tools,
        artifacts=artifacts,
        quarantined=quarantined,
        warnings=warnings,
        next_steps=_next_steps(),
    )


def _next_steps() -> dict[str, str]:
    """How the submitter turns this comprehension into a live MCP surface they host.

    The submitted source is NOT echoed here (it may carry a credential); the self-host
    command is a template the submitter fills with their own spec URL.
    """
    return {
        "self_host": f'uvx --from "{_SELF_HOST_PKG}" gecko <your-openapi-url>',
        "claude_mcp_add": (
            "claude mcp add <name> --transport http https://<your-host>/<name>/mcp"
        ),
        "mcp_json": json.dumps(
            {
                "mcpServers": {
                    "<name>": {
                        "url": "https://<your-host>/<name>/mcp",
                        "transport": "streamable-http",
                    }
                }
            }
        ),
    }


def comprehend_submission(
    source: str,
    *,
    from_docs: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ComprehendResult:
    """Comprehend one submitted API into a control-plane-safe summary + artifacts.

    Flow: SSRF-validate the source → if ``from_docs`` (or an OpenAPI ingest yields no
    operations) run the ``docs_reader`` recovery path, else ingest the OpenAPI → build a
    public (no-auth) client → summarize. Nothing about the submission is retained beyond
    the returned object.

    Raises ``ComprehendError`` (message redacted of any URL credentials) on an unsafe
    source or a document from which no callable surface can be recovered.
    """
    _guard_source(source)
    safe_ref = _redact_url(source) if _is_http_url(source) else source

    if from_docs:
        client, warnings = _from_docs_client(source)
        return _summarize(client, warnings)

    try:
        maybe_client = _openapi_client(source, max_bytes)
    except UnsafeUrlError as exc:
        # A redirect hop into private space, or an oversize document, during the fetch.
        raise ComprehendError(f"could not fetch source: {safe_ref}") from exc
    except ComprehendError:
        raise
    except (ValueError, OSError) as exc:
        raise ComprehendError(f"could not read source: {safe_ref}") from exc

    if maybe_client is not None and maybe_client.operations:
        return _summarize(maybe_client, [])

    # Not a usable OpenAPI — fall back to human-docs recovery (born quarantined).
    logger.info("no OpenAPI operations from %s; trying docs recovery", safe_ref)
    try:
        recovered, warnings = _from_docs_client(source)
    except (ValueError, OSError, UnsafeUrlError) as exc:
        raise ComprehendError(
            f"no OpenAPI or recoverable docs surface at: {safe_ref}"
        ) from exc
    if not recovered.operations:
        raise ComprehendError(f"no callable surface recovered from: {safe_ref}")
    return _summarize(recovered, warnings)
