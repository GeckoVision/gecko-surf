"""The thin typed Orquestra catalog client — offline via the injected http seam.

Every response is untrusted input: sizes capped, shapes validated, ids
pattern-checked, inconsistent surfaces refused. Fixtures under
``tests/fixtures/orquestra/`` are real captured responses (public program
metadata only — control plane, invariant #1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gecko.orquestra_client import (
    MAX_RESPONSE_BYTES,
    OrquestraClient,
    OrquestraClientError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "orquestra"
BASE = "https://api.orquestra.dev/api"
METEORA_SLUG = "v48gsz901w84zriqe0elsl"


def _client(responses: dict[str, bytes]) -> OrquestraClient:
    def http_get(url: str) -> bytes:
        if url not in responses:
            raise AssertionError(f"unexpected GET {url}")
        return responses[url]

    return OrquestraClient(base_url=BASE, http_get=http_get)


def _fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


# -- catalog ----------------------------------------------------------------


def test_list_projects_parses_the_real_catalog_page() -> None:
    client = _client({f"{BASE}/projects?page=1": _fixture_bytes("projects.json")})
    page = client.list_projects()
    assert page.page == 1
    assert page.total_pages > 1  # the catalog is paginated
    assert len(page.projects) == 20
    by_id = {p.id: p for p in page.projects}
    meteora = by_id[METEORA_SLUG]
    assert meteora.program_id == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
    assert meteora.name


def test_list_projects_skips_garbled_rows_but_keeps_good_ones() -> None:
    payload = {
        "projects": [
            {
                "id": "goodslug1",
                "name": "ok",
                "program_id": "So11111111111111111111111111111111111111112",
            },
            {
                "id": "../../etc",
                "name": "traversal",
                "program_id": "So11111111111111111111111111111111111111112",
            },
            {"id": "goodslug2", "name": "bad pid", "program_id": "not-base58!!"},
            "not-a-dict",
        ],
        "pagination": {"page": 1, "totalPages": 1, "total": 4},
    }
    client = _client({f"{BASE}/projects?page=1": json.dumps(payload).encode()})
    page = client.list_projects()
    assert [p.id for p in page.projects] == ["goodslug1"]


def test_catalog_without_projects_list_is_refused() -> None:
    client = _client({f"{BASE}/projects?page=1": b'{"nope": 1}'})
    with pytest.raises(OrquestraClientError, match="projects"):
        client.list_projects()


def test_default_base_url_comes_from_packaged_provider_config() -> None:
    seen: list[str] = []

    def http_get(url: str) -> bytes:
        seen.append(url)
        return json.dumps({"projects": [], "pagination": {}}).encode()

    OrquestraClient(http_get=http_get).list_projects()
    # the engine hardcodes no endpoint — the base URL is provider DATA
    assert seen == ["https://api.orquestra.dev/api/projects?page=1"]


# -- untrusted-input posture ------------------------------------------------


def test_oversized_response_is_refused() -> None:
    client = _client({f"{BASE}/projects?page=1": b"x" * (MAX_RESPONSE_BYTES + 1)})
    with pytest.raises(OrquestraClientError, match="exceeds"):
        client.list_projects()


def test_non_json_response_is_refused() -> None:
    client = _client({f"{BASE}/{METEORA_SLUG}/idl": b"<html>not json</html>"})
    with pytest.raises(OrquestraClientError, match="not JSON"):
        client.project_endpoint(METEORA_SLUG, "idl")


def test_non_object_json_is_refused() -> None:
    client = _client({f"{BASE}/{METEORA_SLUG}/idl": b"[1, 2, 3]"})
    with pytest.raises(OrquestraClientError, match="object"):
        client.project_endpoint(METEORA_SLUG, "idl")


@pytest.mark.parametrize("slug", ["a/b", "../etc", "a b", "", "x" * 65])
def test_path_splicing_slugs_are_rejected(slug: str) -> None:
    client = _client({})
    with pytest.raises(OrquestraClientError, match="slug"):
        client.project_endpoint(slug, "idl")


def test_unknown_project_endpoint_is_rejected() -> None:
    client = _client({})
    with pytest.raises(OrquestraClientError, match="endpoint"):
        client.project_endpoint(METEORA_SLUG, "secrets")


def test_non_http_base_url_is_rejected() -> None:
    with pytest.raises(OrquestraClientError, match="http"):
        OrquestraClient(base_url="file:///etc", http_get=lambda url: b"{}")


# -- fetch_surface ----------------------------------------------------------


def _surface_client(slug: str) -> OrquestraClient:
    return _client(
        {
            f"{BASE}/{slug}/pda": _fixture_bytes(slug, "pda.json"),
            f"{BASE}/{slug}/idl": _fixture_bytes(slug, "idl.json"),
        }
    )


def test_fetch_surface_builds_the_project_surface() -> None:
    surface = _surface_client(METEORA_SLUG).fetch_surface(METEORA_SLUG)
    assert surface.slug == METEORA_SLUG
    assert surface.program_id == "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
    assert surface.idl.get("instructions")
    assert surface.pda_accounts  # Orquestra's own per-instruction seed schemas


def test_fetch_surface_refuses_program_id_mismatch_between_endpoints() -> None:
    pda = json.loads(_fixture_bytes(METEORA_SLUG, "pda.json"))
    pda["programId"] = "So11111111111111111111111111111111111111112"
    client = _client(
        {
            f"{BASE}/{METEORA_SLUG}/pda": json.dumps(pda).encode(),
            f"{BASE}/{METEORA_SLUG}/idl": _fixture_bytes(METEORA_SLUG, "idl.json"),
        }
    )
    with pytest.raises(OrquestraClientError, match="inconsistent"):
        client.fetch_surface(METEORA_SLUG)


def test_fetch_surface_refuses_idl_address_mismatch() -> None:
    idl_payload = json.loads(_fixture_bytes(METEORA_SLUG, "idl.json"))
    idl_payload["idl"]["address"] = "So11111111111111111111111111111111111111112"
    client = _client(
        {
            f"{BASE}/{METEORA_SLUG}/pda": _fixture_bytes(METEORA_SLUG, "pda.json"),
            f"{BASE}/{METEORA_SLUG}/idl": json.dumps(idl_payload).encode(),
        }
    )
    with pytest.raises(OrquestraClientError, match="inconsistent"):
        client.fetch_surface(METEORA_SLUG)


def test_fetch_surface_requires_a_valid_program_id() -> None:
    client = _client(
        {
            f"{BASE}/{METEORA_SLUG}/pda": b'{"pdaAccounts": []}',
            f"{BASE}/{METEORA_SLUG}/idl": _fixture_bytes(METEORA_SLUG, "idl.json"),
        }
    )
    with pytest.raises(OrquestraClientError, match="programId"):
        client.fetch_surface(METEORA_SLUG)
