"""Rendered fetch for JS-hydrated docs — the fallback behind ``gecko from-docs``.

Modern API docs hydrate their nav client-side, so a stdlib fetch gets a shell and the
parser finds nothing. Measured: ``from-docs`` recovered **0 operations** on both Privy and
Birdeye, while a rendered snapshot of the same Privy page exposed **55**. That gap is the
whole reason this module exists.

The renderer shells out to the ``agent-browser`` CLI. It is OPTIONAL: a machine without it
keeps exactly the previous behaviour (static fetch, honest low-op hint) rather than
failing — this is a recall improvement, never a new hard dependency.

**Security — read before changing.** ``safe_get`` validates the URL *and every redirect
hop* before reading. Handing a URL to a browser bypasses that entirely: a browser will
happily fetch ``http://169.254.169.254`` and hand back cloud-instance metadata. So this
module validates the URL with :func:`~gecko.netguard.validate_public_url` BEFORE the
browser is launched, and never renders a URL it did not check. Rendered HTML is still
untrusted input and flows into the same parser, sanitizer, and quarantine path as static
HTML — rendering changes where the bytes came from, not how much we trust them.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable

from ..netguard import UnsafeUrlError, validate_public_url

__all__ = ["RenderError", "Renderer", "agent_browser_render", "default_renderer"]

#: A renderer takes a URL and returns rendered HTML.
Renderer = Callable[[str], str]

#: The CLI we shell out to. Not a Python dependency — it may simply be absent.
_BROWSER_BIN = "agent-browser"

#: Per-subprocess ceiling. A hung browser must never wedge a `from-docs` run.
_STEP_TIMEOUT = 90.0

#: Required in this environment; harmless elsewhere.
_BROWSER_ARGS = "--no-sandbox"


class RenderError(Exception):
    """Rendering failed. Carries the step/reason — never page content."""


def _run(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, URL pre-validated
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def agent_browser_render(url: str, *, timeout: float = _STEP_TIMEOUT) -> str:
    """Open ``url`` in a real browser, wait for hydration, and return the HTML.

    Mirrors the ``read-js-docs`` playbook — open, wait for networkidle, read the body —
    so the skill and the product describe one mechanism instead of drifting apart.
    """
    try:
        validate_public_url(url)
    except UnsafeUrlError as exc:
        # The browser has no SSRF guard of its own; this check IS the guard.
        raise RenderError(f"refusing to render unsafe URL: {exc}") from None
    if shutil.which(_BROWSER_BIN) is None:
        raise RenderError(f"{_BROWSER_BIN} is not installed")

    try:
        opened = _run(
            [_BROWSER_BIN, "open", url, "--args", _BROWSER_ARGS], timeout=timeout
        )
        if opened.returncode != 0:
            raise RenderError(f"{_BROWSER_BIN} open failed (exit {opened.returncode})")
        # Best-effort hydration wait: a page that never reaches networkidle should still
        # be read rather than abandoned, so a non-zero exit here is not fatal.
        _run([_BROWSER_BIN, "wait", "--load", "networkidle"], timeout=timeout)
        got = _run([_BROWSER_BIN, "get", "html", "body"], timeout=timeout)
        if got.returncode != 0:
            raise RenderError(f"{_BROWSER_BIN} get html failed (exit {got.returncode})")
        html = got.stdout or ""
    except subprocess.TimeoutExpired:
        raise RenderError("render timed out") from None
    except OSError as exc:
        raise RenderError(
            f"could not run {_BROWSER_BIN} ({type(exc).__name__})"
        ) from None
    finally:
        # Always close: a leaked browser outlives the CLI and holds the profile lock,
        # so the NEXT from-docs run would fail for an unrelated reason.
        try:
            _run([_BROWSER_BIN, "close", "--all"], timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not html.strip():
        raise RenderError("render returned an empty document")
    return html


def default_renderer() -> Renderer | None:
    """The renderer to use, or ``None`` when ``agent-browser`` is not installed.

    ``None`` is the signal to keep the static-only behaviour — callers must treat a
    missing browser as "no improvement available", never as an error.
    """
    if shutil.which(_BROWSER_BIN) is None:
        return None
    return agent_browser_render
