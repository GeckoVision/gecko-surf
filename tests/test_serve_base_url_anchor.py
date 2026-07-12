"""The `--base-url` follow-up — the whole point of the change.

`gecko add`-wired surfaces serve from a local cache PATH, which ``surfaces.anchor_for``
correctly refuses to trust as pinning provenance (a file on disk is no more trustworthy
than an in-memory dict, and its own ``servers[]`` is attacker-controlled). That left
gecko-add surfaces ``unverified`` -> live auth injection fail-closed, forever.

The fix: `gecko add` reconciles the fetch origin into an explicit ``base_url`` and wires
it into the served ``gecko serve --base-url``. This test proves the anchor actually
flips from unverified -> pinned once that explicit ``base_url`` reaches
``AgentApiClient`` — the regression pin for live-mode auth on gecko-add-wired surfaces.

No network: an in-memory spec dict, no fetch, no DNS.
"""

from __future__ import annotations

from gecko.client import AgentApiClient

_APIKEY_HEADER_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Widget API", "version": "1"},
    "components": {
        "securitySchemes": {
            "apiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key"}
        }
    },
    "paths": {},
}


def test_dict_spec_without_base_url_is_unverified_no_auth_allowed():
    # Mirrors the un-pinned status quo: a cached/dict spec with no provenance at all
    # (no base_url, no spec URL) must fail closed — no host may receive injected auth.
    client = AgentApiClient(_APIKEY_HEADER_SPEC)
    assert client._auth_allowed_hosts == set()
    assert client.anchor.state == "unverified"
    assert client.anchor.may_inject_auth is False


def test_dict_spec_with_explicit_base_url_is_pinned_auth_allowed():
    # Exactly what `gecko add` -> `gecko serve --base-url <fetch-origin>` now does:
    # the same cached spec, but with the out-of-band, dev-supplied base_url reconciled
    # from provenance. The anchor must flip to pinned and allow auth toward that host.
    client = AgentApiClient(_APIKEY_HEADER_SPEC, base_url="https://api.example.com")
    assert client._auth_allowed_hosts == {"api.example.com"}
    assert client.anchor.state == "pinned"
    assert client.anchor.may_inject_auth is True


def test_local_path_spec_without_base_url_stays_unverified(tmp_path):
    # A local cache PATH (what gecko-add's cache_spec produces) is also NOT pinning
    # provenance on its own — same fail-closed rule as a dict.
    import json

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_APIKEY_HEADER_SPEC))
    client = AgentApiClient(str(spec_path))
    assert client._auth_allowed_hosts == set()
    assert client.anchor.state == "unverified"


def test_local_path_spec_with_explicit_base_url_is_pinned(tmp_path):
    import json

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_APIKEY_HEADER_SPEC))
    client = AgentApiClient(str(spec_path), base_url="https://api.example.com")
    assert client._auth_allowed_hosts == {"api.example.com"}
    assert client.anchor.state == "pinned"
    assert client.anchor.may_inject_auth is True
