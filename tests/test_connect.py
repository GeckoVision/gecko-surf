"""``gecko connect`` — the keychain-held bridge to a gated hosted surface.

Pattern B: every guarantee here is falsified OFFLINE. The bridge is exercised with
in-memory anyio streams (no network, no subprocess, no MCP transport), so a regression in
frame forwarding or teardown fails locally rather than during a live smoke.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest

from gecko import connect, credentials
from gecko.credentials import CredentialError, CredentialRef
from gecko.login import IDENTITY_REF

KEY = "gecko_sk_thisisnotarealkey_0123456789"


class _FakeResolver:
    """A ChainResolver stand-in: one slot, or a miss. Light fake over a mock."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def resolve(self, ref: CredentialRef) -> str:
        if self._value is None:
            raise CredentialError(f"no credential for {ref.slot()!r} (tried: keyring)")
        return self._value


def _flatten(exc: BaseException) -> list[BaseException]:
    """Task-group failures surface as an ExceptionGroup; flatten to assert on a leaf."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _flatten(sub)]
    return [exc]


# --- surface_url: the mount name stays a mount name ------------------------------


def test_surface_url_builds_the_gated_mount() -> None:
    assert connect.surface_url("birdeye") == "https://mcp.geckovision.tech/birdeye/mcp"


def test_surface_url_honours_an_explicit_host() -> None:
    assert (
        connect.surface_url("birdeye", host="https://example.com/")
        == "https://example.com/birdeye/mcp"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "../admin",  # traversal out of the mount
        "a/b",  # a path, not a name
        "https://evil.test",  # a whole URL
        "Birdeye",  # uppercase (mounts are lowercase)
        "",
        "   ",
        "-leading-dash",
        "x" * 65,
    ],
)
def test_surface_url_rejects_anything_that_is_not_a_mount_name(bad: str) -> None:
    with pytest.raises(connect.ConnectError, match="invalid surface name"):
        connect.surface_url(bad)


@pytest.mark.parametrize(
    "host",
    [
        "http://127.0.0.1:8000",  # loopback
        "http://192.168.1.10",  # private range
        "http://169.254.169.254",  # link-local (cloud metadata)
        "file:///etc/passwd",  # non-http scheme
    ],
)
def test_surface_url_refuses_to_send_a_bearer_token_somewhere_unsafe(host: str) -> None:
    """--host is user input that we then attach a live credential to, so the standing
    SSRF rule applies: a private/loopback/metadata target must never be dialled."""
    with pytest.raises(connect.ConnectError, match="unsafe host"):
        connect.surface_url("birdeye", host=host)


# --- resolve_key: read the sealed key back, never leak it ------------------------


def test_resolve_key_reads_the_sealed_identity_slot() -> None:
    assert connect.resolve_key(_FakeResolver(KEY)) == KEY  # type: ignore[arg-type]


def test_resolve_key_strips_incidental_whitespace() -> None:
    assert connect.resolve_key(_FakeResolver(f"  {KEY}\n")) == KEY  # type: ignore[arg-type]


def test_resolve_key_missing_names_the_remedy_not_a_value() -> None:
    with pytest.raises(connect.ConnectError) as excinfo:
        connect.resolve_key(_FakeResolver(None))  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "gecko login" in message
    assert IDENTITY_REF.slot() in message
    assert credentials.env_var_name(IDENTITY_REF) in message


def test_resolve_key_rejects_an_empty_sealed_value() -> None:
    """A blank keychain entry must fail loudly, not send `Authorization: Bearer `."""
    with pytest.raises(connect.ConnectError, match="empty"):
        connect.resolve_key(_FakeResolver("   "))  # type: ignore[arg-type]


def test_a_failed_resolve_never_echoes_the_key() -> None:
    class _Leaky:
        def resolve(self, ref: CredentialRef) -> str:
            raise CredentialError("backend exploded")

    with pytest.raises(connect.ConnectError) as excinfo:
        connect.resolve_key(_Leaky())  # type: ignore[arg-type]
    assert KEY not in str(excinfo.value)


def test_auth_headers_is_the_header_the_gate_reads() -> None:
    assert connect.auth_headers(KEY) == {"Authorization": f"Bearer {KEY}"}


# --- bridge: verbatim frames, deterministic teardown -----------------------------


def _streams(size: int = 8) -> tuple[Any, Any]:
    return anyio.create_memory_object_stream(size)  # type: ignore[var-annotated]


def test_bridge_forwards_client_frames_to_the_server() -> None:
    def go() -> Any:
        async def main() -> Any:
            client_in_send, client_read = _streams()
            client_write, _client_out = _streams()
            _server_in_send, server_read = _streams()
            server_write, server_out = _streams()

            await client_in_send.send("initialize")
            # EOF on the client side ends the bridge deterministically: the frame is
            # forwarded first, then teardown cancels the (idle) other direction.
            await client_in_send.aclose()

            await connect.bridge(client_read, client_write, server_read, server_write)
            return server_out.receive_nowait()

        return anyio.run(main)

    assert go() == "initialize"


def test_bridge_forwards_server_frames_to_the_client() -> None:
    def go() -> Any:
        async def main() -> Any:
            _client_in_send, client_read = _streams()
            client_write, client_out = _streams()
            server_in_send, server_read = _streams()
            server_write, _server_out = _streams()

            await server_in_send.send("result")
            await server_in_send.aclose()

            await connect.bridge(client_read, client_write, server_read, server_write)
            return client_out.receive_nowait()

        return anyio.run(main)

    assert go() == "result"


def test_bridge_forwards_frames_verbatim() -> None:
    """The proxy must not inspect or rewrite payloads — it forwards the same object."""
    sentinel = {"jsonrpc": "2.0", "id": 1, "result": {"tools": ["x"]}}

    def go() -> Any:
        async def main() -> Any:
            _cs, client_read = _streams()
            client_write, client_out = _streams()
            server_in_send, server_read = _streams()
            server_write, _so = _streams()
            await server_in_send.send(sentinel)
            await server_in_send.aclose()
            await connect.bridge(client_read, client_write, server_read, server_write)
            return client_out.receive_nowait()

        return anyio.run(main)

    assert go() is sentinel


def test_bridge_ends_when_the_client_disconnects() -> None:
    """Client EOF must tear down both directions, not hang on the open server side."""

    def go() -> None:
        async def main() -> None:
            client_in_send, client_read = _streams()
            client_write, _co = _streams()
            _sis, server_read = _streams()
            server_write, _so = _streams()
            await client_in_send.aclose()
            with anyio.fail_after(5):
                await connect.bridge(
                    client_read, client_write, server_read, server_write
                )

        anyio.run(main)

    go()  # completes (no TimeoutError) => teardown works


def test_bridge_surfaces_a_transport_error_instead_of_dropping_the_frame() -> None:
    """A dropped frame leaves the peer waiting forever; failing loudly is debuggable."""

    def go() -> list[BaseException]:
        async def main() -> None:
            _cs, client_read = _streams()
            client_write, _co = _streams()
            server_in_send, server_read = _streams()
            server_write, _so = _streams()
            await server_in_send.send(ValueError("malformed frame"))
            await server_in_send.aclose()
            await connect.bridge(client_read, client_write, server_read, server_write)

        try:
            anyio.run(main)
        except BaseException as exc:  # noqa: BLE001 - we assert on the leaf below
            return _flatten(exc)
        raise AssertionError("bridge should not have completed cleanly")

    leaves = go()
    assert any(isinstance(leaf, connect.ConnectError) for leaf in leaves)


# --- CLI wiring: stdout is the protocol channel, so it must stay clean -----------


def test_connect_is_a_real_subcommand() -> None:
    """Without this, `gecko connect birdeye` falls through to `serve` and is treated
    as a spec path (the `_default_to_serve` shorthand)."""
    from gecko import cli

    assert "connect" in cli._SUBCOMMANDS


def test_a_connect_failure_never_writes_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Once the bridge runs, stdout carries JSON-RPC frames. A stray diagnostic on
    stdout would corrupt the stream, so every error goes to stderr — asserted on the
    one failure path that is reachable offline (bad mount name, no network, no key)."""
    from gecko import cli

    code = cli.main(["connect", "../admin"])
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "invalid surface name" in captured.err


# --- the headless fallback must be settable in a real shell ----------------------


def test_the_identity_env_fallback_is_a_valid_shell_identifier() -> None:
    """`gecko connect` tells a headless user to export this name. A hyphen in it
    (GECKO_CRED_GECKO-IDENTITY) cannot be exported by any POSIX shell, so the
    documented fallback would be dead on arrival."""
    name = credentials.env_var_name(IDENTITY_REF)
    assert "-" not in name
    assert name == "GECKO_CRED_GECKO_IDENTITY"


def test_the_identity_key_resolves_from_the_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(credentials.env_var_name(IDENTITY_REF), KEY)
    resolver = credentials.ChainResolver(backends=[credentials.EnvBackend()])
    assert connect.resolve_key(resolver) == KEY


def test_the_pre_normalization_hyphen_name_still_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Programmatically-set env (Docker -e, an MCP client's env block) may still
    carry the old hyphenated name — reading it must not regress."""
    monkeypatch.setenv("GECKO_CRED_GECKO-IDENTITY", KEY)
    monkeypatch.delenv("GECKO_CRED_GECKO_IDENTITY", raising=False)
    resolver = credentials.ChainResolver(backends=[credentials.EnvBackend()])
    assert connect.resolve_key(resolver) == KEY


# --- a rejected key must be one legible line, not a silent hang ------------------


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status)


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_reported_as_a_rejected_key(status: int) -> None:
    """The real failure mode: the transport raises inside a task group, the process
    dies with no JSON-RPC response, and the MCP client waits forever on initialize."""
    err = connect.terminal_error(BaseExceptionGroup("tg", [_HttpError(status)]))
    assert isinstance(err, connect.ConnectError)
    assert str(status) in str(err)
    assert "gecko login" in str(err)


def test_other_http_statuses_are_reported_with_their_code() -> None:
    err = connect.terminal_error(BaseExceptionGroup("tg", [_HttpError(502)]))
    assert "502" in str(err)


def test_a_non_http_failure_names_only_the_exception_type() -> None:
    err = connect.terminal_error(BaseExceptionGroup("tg", [OSError("no route")]))
    assert "OSError" in str(err)


def test_a_mapped_transport_error_never_leaks_the_key_or_headers() -> None:
    leaky = _HttpError(403)
    leaky.args = (f"Authorization: Bearer {KEY}",)
    err = connect.terminal_error(BaseExceptionGroup("tg", [leaky]))
    assert KEY not in str(err)
    assert "Authorization" not in str(err)


def test_nested_exception_groups_are_flattened() -> None:
    nested = BaseExceptionGroup(
        "outer", [BaseExceptionGroup("inner", [_HttpError(403)])]
    )
    assert "403" in str(connect.terminal_error(nested))


# --- teardown: the sinks must close or the transports never exit -----------------


def test_bridge_closes_both_sinks_so_the_transports_can_exit() -> None:
    """The regression. Each MCP transport runs a writer task that loops over its write
    stream; its context manager will not exit while that task lives. The bridge held
    both sinks open, so on stdin EOF the bridge returned but `stdio_server.__aexit__`
    waited forever — and since a client closes stdin to shut a server down, every
    client restart leaked an orphaned `gecko connect` process.

    The original teardown test passed because in-memory streams do not model that
    dependency: nothing was waiting on the sink, so nothing hung.
    """

    def go() -> tuple[bool, bool]:
        async def main() -> tuple[bool, bool]:
            client_in_send, client_read = _streams()
            client_write, _co = _streams()
            _sis, server_read = _streams()
            server_write, _so = _streams()
            await client_in_send.aclose()
            await connect.bridge(client_read, client_write, server_read, server_write)

            async def is_closed(stream: Any) -> bool:
                try:
                    await stream.send("probe")
                except anyio.ClosedResourceError:
                    return True
                return False

            return await is_closed(client_write), await is_closed(server_write)

        return anyio.run(main)

    client_closed, server_closed = go()
    assert client_closed, "client_write left open — stdio_server would never exit"
    assert server_closed, "server_write left open — the http transport would never exit"


def test_a_transport_that_waits_on_its_writer_still_shuts_down() -> None:
    """Models the real dependency end-to-end: a fake transport whose teardown blocks
    until its write stream closes. Hangs (and fails) if the bridge stops closing sinks."""

    def go() -> None:
        async def main() -> None:
            client_in_send, client_read = _streams()
            client_write, client_write_reader = _streams()
            _sis, server_read = _streams()
            server_write, _so = _streams()
            await client_in_send.aclose()

            async def transport_writer() -> None:
                # Exactly what stdio_server's writer does: drain until the sink closes.
                async for _item in client_write_reader:
                    pass

            with anyio.fail_after(5):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(transport_writer)
                    await connect.bridge(
                        client_read, client_write, server_read, server_write
                    )

        anyio.run(main)

    go()  # a TimeoutError here means the leak is back


# --- diagnostics: a connection failure must be debuggable, not opaque -------------


def test_terminal_error_surfaces_the_real_connection_reason() -> None:
    """The bug it fixes: "could not reach the hosted surface (ConnectError)" told a user
    NOTHING. The httpx message ("Name or service not known", a cert error) is the detail
    that distinguishes a wrong host from a TLS intercept from a firewall — and it carries
    no secret."""

    class _ConnError(Exception):
        pass

    err = connect.terminal_error(
        BaseExceptionGroup("tg", [_ConnError("[Errno -2] Name or service not known")])
    )
    msg = str(err)
    assert "could not reach" in msg
    assert "Name or service not known" in msg  # the actionable detail


def test_a_surfaced_reason_never_leaks_a_key() -> None:
    class _Err(Exception):
        pass

    err = connect.terminal_error(
        BaseExceptionGroup("tg", [_Err(f"connecting with {KEY} failed")])
    )
    assert KEY not in str(err)
    assert "gecko_sk_<redacted>" in str(err)


# --- --probe self-test: verify the path from a terminal, no MCP client -----------


def test_probe_fails_fast_without_a_key_no_network() -> None:
    """`--probe` must surface a missing-key error before any network — a resolver miss
    maps to the same ConnectError as `connect`, so the terminal self-test explains itself
    instead of hanging or a stack trace."""

    class _NoKey:
        def resolve(self, ref):
            raise CredentialError("no credential")

    with pytest.raises(connect.ConnectError, match="no Gecko key sealed"):
        connect.probe("birdeye", resolver=_NoKey())  # type: ignore[arg-type]


def test_probe_rejects_a_bad_surface_before_touching_the_network() -> None:
    with pytest.raises(connect.ConnectError, match="invalid surface name"):
        connect.probe("../admin", resolver=_FakeResolver(KEY))  # type: ignore[arg-type]


def test_cli_probe_flag_is_wired_and_prints_to_stderr(capsys) -> None:
    """`gecko connect <bad> --probe` uses the probe path and reports on stderr (stdout is
    the protocol channel), exit 2 on failure — never hangs like bare serve."""
    from gecko import cli

    code = cli.main(["connect", "../admin", "--probe"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "invalid surface name" in captured.err
