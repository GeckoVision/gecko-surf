"""The public SDK surface — `from surfcall import ...`.

Builders embed surfcall as a library; these top-level imports are the contract.
They must resolve engine-only (no `serve`/`sosbot` extras installed), so nothing
exported here may pull in mcp/uvicorn/anthropic at import time.
"""

from __future__ import annotations


def test_sdk_symbols_exported_from_package_root() -> None:
    # The contract a builder relies on.
    from surfcall import (
        AgentApiClient,
        McpSurface,
        NoAuthSession,
        Session,
        public_session,
    )

    # Same objects as their defining modules — re-export, not a shadow copy.
    from surfcall.access import NoAuthSession as _NoAuth
    from surfcall.access import Session as _Session
    from surfcall.access import public_session as _public_session
    from surfcall.client import AgentApiClient as _Client
    from surfcall.mcp_server import McpSurface as _Surface

    assert AgentApiClient is _Client
    assert McpSurface is _Surface
    assert public_session is _public_session
    assert Session is _Session
    assert NoAuthSession is _NoAuth


def test_all_declares_the_public_surface() -> None:
    import surfcall

    for name in (
        "AgentApiClient",
        "McpSurface",
        "Session",
        "NoAuthSession",
        "public_session",
        "__version__",
    ):
        assert name in surfcall.__all__, f"{name} missing from __all__"


def test_console_entry_point_is_importable_and_callable() -> None:
    # Backs `[project.scripts] surfcall = "surfcall.serve:_run"`.
    from surfcall.serve import _run, main

    assert callable(_run)
    assert callable(main)
