"""The /healthz route the ECS ALB target group health-checks.

It must return a plain 200 and bypass the DNS-rebinding guard — the ALB sends
Host: <task-private-ip>:8000, which the /mcp allowlist would reject, so /healthz
deliberately lives outside the guarded transport.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import httpx
import pytest

pytest.importorskip("mcp")  # skip cleanly without the serve extra

from gecko.http_server import build_http_app  # noqa: E402

PEGANA = str(Path(__file__).resolve().parent / "fixtures" / "pegana_openapi.json")


def _get_healthz(app: object, host: str) -> tuple[int, str]:
    async def body() -> tuple[int, str]:
        async with app.router.lifespan_context(app):  # type: ignore[attr-defined]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=f"http://{host}"
            ) as client:
                r = await client.get("/healthz")
                return r.status_code, r.text

    return anyio.run(body)


def test_healthz_returns_plain_200_ok():
    app = build_http_app(
        PEGANA, mode="recorded", allowed_hosts=["test"], allowed_origins=["http://test"]
    )
    code, text = _get_healthz(app, "test")
    assert code == 200 and text == "ok"


def test_healthz_bypasses_the_host_allowlist():
    # A restrictive allowlist that would reject /mcp must NOT block /healthz — the
    # ALB health check arrives with an unallowlisted Host (the task private IP).
    app = build_http_app(
        PEGANA, mode="recorded", allowed_hosts=["only-this"], allowed_origins=[]
    )
    code, _ = _get_healthz(app, "10.0.1.23:8000")
    assert code == 200
