"""The public SDK surface — `from gecko import ...`.

Builders embed Gecko as a library; these top-level imports are the contract.
They must resolve engine-only (no `serve`/`sosbot` extras installed), so nothing
exported here may pull in mcp/uvicorn/anthropic at import time.
"""

from __future__ import annotations


def test_sdk_symbols_exported_from_package_root() -> None:
    # The contract a builder relies on.
    from gecko import (
        AgentApiClient,
        McpSurface,
        NoAuthSession,
        Session,
        public_session,
    )

    # Same objects as their defining modules — re-export, not a shadow copy.
    from gecko.access import NoAuthSession as _NoAuth
    from gecko.access import Session as _Session
    from gecko.access import public_session as _public_session
    from gecko.client import AgentApiClient as _Client
    from gecko.mcp_server import McpSurface as _Surface

    assert AgentApiClient is _Client
    assert McpSurface is _Surface
    assert public_session is _public_session
    assert Session is _Session
    assert NoAuthSession is _NoAuth


def test_all_declares_the_public_surface() -> None:
    import gecko

    for name in (
        "AgentApiClient",
        "McpSurface",
        "Session",
        "NoAuthSession",
        "public_session",
        "__version__",
    ):
        assert name in gecko.__all__, f"{name} missing from __all__"


def test_console_entry_point_is_importable_and_callable() -> None:
    # Backs `[project.scripts] gecko = "gecko.cli:_run"` (the subcommand dispatcher).
    from gecko.cli import _run, main

    assert callable(_run)
    assert callable(main)

    # `python -m gecko.serve` must keep working too (backward-compat).
    from gecko.serve import _run as serve_run
    from gecko.serve import main as serve_main

    assert callable(serve_run)
    assert callable(serve_main)
