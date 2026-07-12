import json
import pytest
from gecko.netguard import UnsafeUrlError
from gecko.onboard import resolve_spec, OnboardError

_SPEC = {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": {}}


def _resolver(mapping: dict[str, list[str]]):
    """A fake DNS resolver: host -> list of IP strings."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _resolver({"api.example.com": ["93.184.216.34"]})


def test_resolves_openapi_url_via_injected_fetch():
    spec = resolve_spec(
        "https://api.example.com/openapi.json",
        fetch=lambda u: json.dumps(_SPEC),
        resolver=PUBLIC,
    )
    assert spec["openapi"] == "3.0.3"


def test_resolves_local_path(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_SPEC))
    assert resolve_spec(str(p))["info"]["title"] == "T"


def test_rejects_unsafe_url():
    with pytest.raises(OnboardError):
        resolve_spec("http://169.254.169.254/openapi.json", fetch=lambda u: "{}")
