import json
import pytest
from gecko.netguard import UnsafeUrlError
from gecko.onboard import discover_spec, resolve_spec, pin_base_url, OnboardError

_SPEC = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}}


def _multi_fetch(mapping: dict[str, str]):
    """Fake fetch: url -> body; an unknown url raises (like a 404)."""

    def fetch(url: str) -> str:
        if url not in mapping:
            raise OSError(f"404 {url}")
        return mapping[url]

    return fetch


def _resolver(mapping: dict[str, list[str]]):
    """A fake DNS resolver: host -> list of IP strings."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _resolver({"api.example.com": ["93.184.216.34"]})


def test_resolves_openapi_url_via_injected_fetch():
    resolved = resolve_spec(
        "https://api.example.com/openapi.json",
        fetch=lambda u: json.dumps(_SPEC),
        resolver=PUBLIC,
    )
    assert resolved.spec["openapi"] == "3.0.3"
    # http(s) URL that yielded a direct JSON OpenAPI -> the fetch origin IS provenance.
    assert resolved.spec_url == "https://api.example.com/openapi.json"


def test_resolves_local_path(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_SPEC))
    resolved = resolve_spec(str(p))
    assert resolved.spec["info"]["title"] == "T"
    # A local path is never pinning provenance (see surfaces.anchor_for).
    assert resolved.spec_url is None


def test_rejects_unsafe_url():
    with pytest.raises(OnboardError):
        resolve_spec("http://169.254.169.254/openapi.json", fetch=lambda u: "{}")


# --- auto-discovery: `gecko add <domain>` finds the spec ----------------------------


def test_discovers_spec_at_common_path_from_bare_domain():
    # Root is HTML, but /openapi.json is a spec — discovery finds it.
    fetch = _multi_fetch(
        {
            "https://api.example.com": "<html>docs</html>",
            "https://api.example.com/openapi.json": json.dumps(_SPEC),
        }
    )
    resolved = resolve_spec("https://api.example.com", fetch=fetch, resolver=PUBLIC)
    assert resolved.spec["openapi"] == "3.0.3"
    # the DISCOVERED url is the provenance we pin to, not the bare domain
    assert resolved.spec_url == "https://api.example.com/openapi.json"


def test_discovery_probes_past_missing_paths_to_swagger():
    fetch = _multi_fetch(
        {
            "https://api.example.com": "<html/>",
            "https://api.example.com/swagger.json": json.dumps(_SPEC),
        }
    )
    resolved = resolve_spec("https://api.example.com", fetch=fetch, resolver=PUBLIC)
    assert resolved.spec_url == "https://api.example.com/swagger.json"


def test_direct_spec_url_short_circuits_without_probing():
    calls: list[str] = []

    def fetch(url: str) -> str:
        calls.append(url)
        return json.dumps(_SPEC)

    resolved = resolve_spec(
        "https://api.example.com/openapi.json", fetch=fetch, resolver=PUBLIC
    )
    assert resolved.spec_url == "https://api.example.com/openapi.json"
    assert calls == [
        "https://api.example.com/openapi.json"
    ]  # exact ref only, no probes


def test_discover_spec_ssrf_blocks_every_probe():
    # A resolver that resolves nothing -> every probe fails validate_public_url -> None.
    result = discover_spec(
        "https://api.example.com",
        fetch=lambda u: json.dumps(_SPEC),
        resolver=_resolver({}),
    )
    assert result is None


def test_discovery_falls_back_to_docs_when_nothing_found(monkeypatch):
    import gecko.onboard as onboard

    fetch = _multi_fetch({"https://api.example.com": "<html>docs</html>"})

    class _Draft:
        draft = {
            "openapi": "3.0.3",
            "info": {"title": "recovered", "version": "1"},
            "paths": {},
        }

    monkeypatch.setattr(onboard.docs_reader, "from_docs", lambda ref: _Draft())
    resolved = resolve_spec("https://api.example.com", fetch=fetch, resolver=PUBLIC)
    assert resolved.spec["info"]["title"] == "recovered"
    assert resolved.spec_url is None  # docs-recovery -> unpinned


# --- bare-domain refs: `gecko add api.example.com` (no scheme) -----------------------


def test_bare_domain_retries_as_https_through_discovery():
    # The Pegana field repro: a schemeless domain must NOT be read as a local file —
    # it re-enters the SAME https URL/discovery pipeline as an explicit URL.
    fetch = _multi_fetch(
        {
            "https://api.example.com": "<html>docs</html>",
            "https://api.example.com/openapi.json": json.dumps(_SPEC),
        }
    )
    resolved = resolve_spec("api.example.com", fetch=fetch, resolver=PUBLIC)
    assert resolved.spec["openapi"] == "3.0.3"
    assert resolved.spec_url == "https://api.example.com/openapi.json"


def test_bare_domain_with_path_retries_as_https():
    resolved = resolve_spec(
        "api.example.com/openapi.json",
        fetch=_multi_fetch({"https://api.example.com/openapi.json": json.dumps(_SPEC)}),
        resolver=PUBLIC,
    )
    assert resolved.spec_url == "https://api.example.com/openapi.json"


def test_bare_domain_is_ssrf_validated_before_any_fetch():
    # The https retry goes through validate_public_url like any other URL — a
    # link-local IP literal is refused without a single byte fetched.
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return json.dumps(_SPEC)

    with pytest.raises(OnboardError):
        resolve_spec("169.254.169.254", fetch=fetch)
    assert fetched == []


def test_existing_file_named_like_a_domain_wins_over_https(tmp_path, monkeypatch):
    # Files win: a ref that exists on disk is read as a file — no network at all.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "api.example.com").write_text(json.dumps(_SPEC))
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        raise OSError("must not fetch")

    resolved = resolve_spec("api.example.com", fetch=fetch, resolver=PUBLIC)
    assert resolved.spec["info"]["title"] == "T"
    assert resolved.spec_url is None  # a local file is never pinning provenance
    assert fetched == []


def test_nonexistent_non_domain_ref_reports_both_interpretations(tmp_path):
    # Neither a file nor a reachable https host: the error must name BOTH attempts
    # so the next Raff sees what was tried instead of a bare ENOENT.
    missing = str(tmp_path / "nope" / "spec.json")
    with pytest.raises(OnboardError) as excinfo:
        resolve_spec(missing, fetch=_multi_fetch({}), resolver=_resolver({}))
    msg = str(excinfo.value)
    assert "local file" in msg  # interpretation 1: a file — not found
    assert f"https://{missing}" in msg  # interpretation 2: the https probe — failed
    assert missing in msg


def test_explicit_http_scheme_is_untouched_not_rewritten():
    # A ref WITH a scheme keeps current behavior: explicit http:// stays http.
    resolved = resolve_spec(
        "http://api.example.com/openapi.json",
        fetch=lambda u: json.dumps(_SPEC),
        resolver=PUBLIC,
    )
    assert resolved.spec_url == "http://api.example.com/openapi.json"


def test_non_http_scheme_keeps_local_read_error_without_fetching():
    # ftp:// / file:// etc. never had URL handling; they keep the local-path error
    # and never gain a network attempt.
    fetched: list[str] = []

    def fetch(url: str) -> str:
        fetched.append(url)
        return json.dumps(_SPEC)

    with pytest.raises(OnboardError) as excinfo:
        resolve_spec("ftp://api.example.com/spec.json", fetch=fetch)
    assert "could not read spec at" in str(excinfo.value)
    assert fetched == []


# --- pin_base_url: reconcile the fetch origin against the spec's own servers[] -------


def test_pin_base_url_none_when_no_spec_url():
    # docs-recovery / local-path sources stay unverified — CORRECT, do not pin.
    base_url, warning = pin_base_url(None, _SPEC)
    assert base_url is None
    assert warning is None


def test_pin_base_url_trusts_servers_url_when_host_matches_fetch_origin():
    spec = {**_SPEC, "servers": [{"url": "https://api.example.com/v2"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    # Host matches provenance -> trust the full server URL (keeps any path prefix).
    assert base_url == "https://api.example.com/v2"
    assert warning is None


def test_pin_base_url_falls_back_to_fetch_origin_when_host_mismatches():
    spec = {**_SPEC, "servers": [{"url": "https://evil.example.net/v2"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    # NEVER trust the spec's own server host when it disagrees with provenance.
    assert base_url == "https://api.example.com"
    assert warning is not None
    assert "evil.example.net" in warning
    assert "api.example.com" in warning


def test_pin_base_url_falls_back_to_fetch_origin_when_no_servers():
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", _SPEC)
    assert base_url == "https://api.example.com"
    assert warning is not None
    assert "api.example.com" in warning


# --- hardening: relative & same-host server URLs keep only path, force trusted scheme/host/port -------


def test_pin_base_url_relative_server_url():
    # Relative server URL: extract path and combine with trusted origin.
    spec = {**_SPEC, "servers": [{"url": "/v1"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    assert base_url == "https://api.example.com/v1"
    assert warning is None


def test_pin_base_url_same_host_http_downgrade_attempt():
    # Same-host absolute with downgraded scheme: force scheme from trusted origin.
    spec = {**_SPEC, "servers": [{"url": "http://api.example.com/v2"}]}
    base_url, warning = pin_base_url("https://api.example.com/spec.json", spec)
    assert base_url == "https://api.example.com/v2"
    assert warning is None


def test_pin_base_url_same_host_port_change_attempt():
    # Same-host absolute with malicious port: force host+port from trusted origin.
    spec = {**_SPEC, "servers": [{"url": "https://api.example.com:1337/v1"}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    assert base_url == "https://api.example.com/v1"
    assert warning is None


def test_pin_base_url_relative_empty_path():
    # Relative server URL with empty path: just return the origin.
    spec = {**_SPEC, "servers": [{"url": ""}]}
    base_url, warning = pin_base_url("https://api.example.com/openapi.json", spec)
    assert base_url == "https://api.example.com"
    assert warning is None
