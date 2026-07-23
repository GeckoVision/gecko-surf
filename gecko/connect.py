"""``gecko connect <surface>`` — reach a GATED hosted surface with the Gecko key held
in the OS keychain, so no secret ever lands in an MCP client config.

The gap this closes: a hosted gated mount (``/birdeye/mcp``) authenticates with
``Authorization: Bearer gecko_sk_…``. Every MCP client sends static headers from its
config file, so using one meant pasting the key into ``~/.claude.json`` in plaintext.
``gecko login`` already seals a server-minted key into the OS keychain
(:data:`gecko.login.IDENTITY_REF`) without ever printing it — but nothing read it back.

This module is that reader. It runs as the client's stdio MCP server, resolves the key
through the normal credential chain (keychain -> env), and bridges JSON-RPC frames to the
hosted Streamable-HTTP mount. The client config then carries a command, not a credential::

    {"mcpServers": {"gecko-birdeye": {"command": "gecko", "args": ["connect", "birdeye"]}}}

Transparent at the frame level: it forwards :class:`SessionMessage` objects verbatim and
never inspects, rewrites, or stores them. That keeps invariant #1 intact — the proxy sees
response payloads in flight but persists nothing — and means it needs no knowledge of
tools/prompts/resources, so a server-side capability change needs no change here.

Redact-before-raise: the key appears only in the ``Authorization`` header handed to the
transport. It is never logged, never placed in an error, and never written to disk.
"""

from __future__ import annotations

import re
import sys
from typing import Any

from . import credentials
from .credentials import ChainResolver, CredentialError
from .login import IDENTITY_REF
from .netguard import UnsafeUrlError, validate_public_url

#: The hosted plane. Overridable with ``--host`` (validated the same way).
DEFAULT_HOST = "https://mcp.geckovision.tech"

#: A mount name, not a path. Anchored and punctuation-free so a crafted argument
#: (``../admin``, ``a/b``, a scheme) can never escape its mount or retarget the URL.
_SURFACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Redaction for any Gecko key that could appear in a surfaced error message.
_SK_RE = re.compile(r"gecko_sk_[A-Za-z0-9]+")


class ConnectError(Exception):
    """A connect-path failure. NEVER carries a key — only the ref, host, or reason."""


def surface_url(surface: str, *, host: str = DEFAULT_HOST) -> str:
    """The hosted MCP endpoint for ``surface``.

    Both halves are validated before they are joined: the surface against
    :data:`_SURFACE_RE` (so it stays a mount name), and the host through
    :func:`~gecko.netguard.validate_public_url` (no private ranges, loopback,
    link-local, or non-http schemes — the standing SSRF rule, which applies here because
    ``--host`` is user input that we then send a bearer token to).
    """
    name = (surface or "").strip()
    if not _SURFACE_RE.match(name):
        raise ConnectError(
            f"invalid surface name {name!r} — expected a mount name like 'birdeye' "
            "(lowercase letters, digits, '-' or '_')"
        )
    base = (host or "").strip().rstrip("/")
    try:
        validate_public_url(base)
    except UnsafeUrlError as exc:
        raise ConnectError(f"refusing unsafe host: {exc}") from exc
    return f"{base}/{name}/mcp"


def resolve_key(resolver: ChainResolver | None = None) -> str:
    """The sealed Gecko key, via the normal chain (keychain -> env -> command).

    Raises :class:`ConnectError` with remediation that names the slot and the env var
    fallback — never a value, and never a partial/prefixed key.
    """
    chain = resolver if resolver is not None else credentials.default_resolver()
    try:
        key = chain.resolve(IDENTITY_REF)
    except CredentialError as exc:
        raise ConnectError(
            f"no Gecko key sealed for {IDENTITY_REF.slot()!r}. Run `gecko login` "
            f"(seals it in the OS keychain; it is never shown), or set "
            f"{credentials.env_var_name(IDENTITY_REF)} for a headless box. ({exc})"
        ) from exc
    key = key.strip()
    if not key:
        raise ConnectError(
            f"the sealed credential for {IDENTITY_REF.slot()!r} is empty — "
            "re-run `gecko login`."
        )
    return key


def auth_headers(key: str) -> dict[str, str]:
    """The single header the gate reads (``http_server._bearer_from_scope``)."""
    return {"Authorization": f"Bearer {key}"}


async def bridge(
    client_read: Any,
    client_write: Any,
    server_read: Any,
    server_write: Any,
) -> None:
    """Pump JSON-RPC frames both ways until either side ends.

    Verbatim forwarding: whatever :class:`SessionMessage` arrives is what is sent on. No
    inspection, no rewriting, no buffering to disk.

    A transport-level error (the streams yield ``SessionMessage | Exception``) tears the
    whole bridge down rather than being skipped. Dropping a frame would leave the peer
    waiting on a response that can never arrive — a silent hang is far worse to debug
    than a closed session.
    """
    import anyio

    async with anyio.create_task_group() as tg:

        async def one_way(source: Any, sink: Any, direction: str) -> None:
            async for item in source:
                if isinstance(item, Exception):
                    raise ConnectError(
                        f"transport error while forwarding {direction}: "
                        f"{type(item).__name__}"
                    ) from item
                await sink.send(item)
            # EOF one way (the client exited, or the server closed the session) makes
            # the other direction pointless — tear both down instead of hanging.
            tg.cancel_scope.cancel()

        tg.start_soon(one_way, client_read, server_write, "client->server")
        tg.start_soon(one_way, server_read, client_write, "server->client")

    # Both directions are finished, so close the sinks we were writing into. This is
    # NOT tidiness — each transport runs a writer task that loops over its write
    # stream, and its context manager will not exit while that task is alive. Holding
    # these open hung the whole process on stdin EOF: the bridge returned, but
    # `stdio_server.__aexit__` waited forever for a writer that could never end. An MCP
    # client closes stdin to shut a server down, so every client restart leaked an
    # orphaned `gecko connect`.
    for sink in (server_write, client_write):
        with anyio.CancelScope(shield=True):
            await sink.aclose()


def _flatten(exc: BaseException) -> list[BaseException]:
    """Task-group failures arrive as a nested ExceptionGroup; flatten to the leaves."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _flatten(sub)]
    return [exc]


def terminal_error(exc: BaseException) -> ConnectError:
    """Map a transport failure to a redacted, actionable :class:`ConnectError`.

    Without this the underlying ``httpx.HTTPStatusError`` escapes as a raw
    ExceptionGroup: the MCP client gets a crashed process and no JSON-RPC response, so
    it sits waiting on ``initialize`` instead of reporting an auth failure. A rejected
    key is the single most likely failure here and it must say so in one line.

    Only the status code and exception TYPE names are surfaced — never a header, a
    response body, or the key.
    """
    leaves = _flatten(exc)
    for leaf in leaves:
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        if status in (401, 403):
            return ConnectError(
                f"the hosted surface rejected this Gecko key (HTTP {status}). "
                "Run `gecko login` to seal a current one, or ask for your account to "
                "be enabled for this surface."
            )
        if isinstance(status, int):
            return ConnectError(f"the hosted surface returned HTTP {status}.")
    # Connection-level failure (DNS, TLS, timeout, refused). The httpx message names the
    # REAL reason ("Name or service not known", a cert error, "Connection refused") — the
    # detail needed to tell a wrong host from a TLS intercept from a firewall. It carries
    # no secret (the key lives in a header httpx never echoes here), but scrub any
    # gecko_sk_ token defensively and cap length before surfacing.
    detail = "; ".join(_reason(leaf) for leaf in leaves if _reason(leaf))
    suffix = f" — {detail}" if detail else ""
    return ConnectError(f"could not reach the hosted surface{suffix}.")


def _reason(leaf: BaseException) -> str:
    """A redacted, capped one-line reason for a connection error: ``Type: message``."""
    message = _SK_RE.sub("gecko_sk_<redacted>", str(leaf)).strip()
    name = type(leaf).__name__
    return f"{name}: {message}" if message else name


async def serve_connect(url: str, headers: dict[str, str]) -> None:
    """Own both transports and bridge them. Imports are local so the CLI's other
    subcommands never pay for the MCP transport stack."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.stdio import stdio_server

    async with streamablehttp_client(url, headers=headers) as (
        server_read,
        server_write,
        _get_session_id,
    ):
        async with stdio_server() as (client_read, client_write):
            await bridge(client_read, client_write, server_read, server_write)


def connect(
    surface: str,
    *,
    host: str = DEFAULT_HOST,
    resolver: ChainResolver | None = None,
) -> None:
    """Resolve the sealed key and serve ``surface`` over stdio. Blocks until EOF."""
    import anyio

    url = surface_url(surface, host=host)
    # Announce the target on STDERR (stdout is the JSON-RPC channel). Not a secret — it's
    # the public mount URL — and it answers "which host is it hitting?" at a glance, which
    # is the first question when a connection fails.
    print(f"gecko connect → {url}", file=sys.stderr, flush=True)
    headers = auth_headers(resolve_key(resolver))
    try:
        anyio.run(serve_connect, url, headers)
    except (KeyboardInterrupt, SystemExit):
        raise
    except ConnectError:
        raise
    except BaseException as exc:  # noqa: BLE001 - mapped to a redacted ConnectError
        # `from None`: the transport traceback is noise on a channel whose stdout is
        # the protocol stream, and the caller needs the one-line cause, not 40 frames.
        raise terminal_error(exc) from None
