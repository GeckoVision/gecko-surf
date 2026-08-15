"""`/version` — which code is actually live.

`/healthz` answers "is the process up", which is a weaker question than it looks. Twice in
one session the only way to establish whether a merged change had reached the hosted
surface was to call a tool and read the shape of its error, and the first reading of that
was wrong — a merged-but-undeployed fix was reported as deployed.

`tools_rev` is a content hash of the SERVED tool set. Unlike a version string or a git SHA
baked at build time, it is derived from the thing itself, so it cannot claim a surface the
server is not actually offering. Comparing it against a local build turns "is live current?"
from an inference into an equality.

Control plane only: hashes, counts and tool NAMES. No spec text, no descriptions, no
credentials, no client data.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from gecko.http_server import build_http_app  # noqa: E402
from gecko.surfaces import tools_rev  # noqa: E402

SPEC = "tests/fixtures/pegana_openapi.json"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(build_http_app(SPEC))


def test_version_reports_the_served_tools_rev(client: TestClient) -> None:
    body = client.get("/version").json()
    listed = client.get("/version").json()["tool_names"]
    assert body["tool_count"] == len(listed) > 0
    # The rev must be a hash OF THE SERVED TOOLS, not a constant: rebuild the same surface
    # and it matches, which is the whole point of using it as a deploy check.
    from gecko.http_server import _surface_from  # noqa: PLC0415

    rebuilt = tools_rev(list(_surface_from(SPEC, None, "recorded", None).list_tools()))
    assert body["tools_rev"] == rebuilt


def test_version_changes_when_the_served_surface_changes() -> None:
    """A deploy check that reports the same value for two different surfaces is useless."""
    a = TestClient(build_http_app(SPEC)).get("/version").json()
    b = (
        TestClient(build_http_app("tests/fixtures/txodds_docs.yaml"))
        .get("/version")
        .json()
    )
    assert a["tools_rev"] != b["tools_rev"]
    assert a["tool_names"] != b["tool_names"]


def test_version_leaks_no_spec_prose(client: TestClient) -> None:
    """Names and hashes only. A description is untrusted ingested text and has no business
    on an unauthenticated endpoint."""
    raw = json.dumps(client.get("/version").json())
    assert set(client.get("/version").json()) == {
        "tools_rev",
        "tool_count",
        "tool_names",
        "package_version",
    }
    surface = _tools(client)
    for tool in surface:
        description = tool.get("description") or ""
        if len(description) > 40:
            assert description[:40] not in raw, (
                "a tool description leaked into /version"
            )


def test_healthz_is_untouched(client: TestClient) -> None:
    """The ALB target-group check matches on this route; `/version` exists precisely so
    `/healthz` did not have to change shape."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"


def _tools(client: TestClient) -> list[dict]:
    from gecko.http_server import _surface_from  # noqa: PLC0415

    return list(_surface_from(SPEC, None, "recorded", None).list_tools())
