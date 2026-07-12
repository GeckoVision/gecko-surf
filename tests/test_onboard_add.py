import json

from gecko.netguard import UnsafeUrlError
from gecko.onboard import AddDeps, add, safe_name

_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Stripe", "version": "1"},
    "components": {"securitySchemes": {"k": {"type": "apiKey"}}},
    "paths": {},
}

_NO_AUTH_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Open", "version": "1"},
    "paths": {},
}


def _fake_resolver(mapping: dict[str, list[str]]):
    """A fake DNS resolver: host -> list of IP strings. No real network."""

    def resolve(host: str) -> list[str]:
        if host not in mapping:
            raise UnsafeUrlError(f"unresolvable test host: {host}")
        return mapping[host]

    return resolve


PUBLIC = _fake_resolver({"api.stripe.com": ["93.184.216.34"]})


def test_add_end_to_end_with_fakes(tmp_path, capsys):
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: calls.append(("store", n)) or True,
        run=lambda cmd: calls.append(("run", cmd)) or 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert ("store", "api-stripe-com") in calls
    assert (tmp_path / ".gecko" / "surfaces" / "api-stripe-com.json").exists()
    assert "47" in out and "ask your agent" in out.lower()
    assert "sealed" in out.lower()


def test_add_resolve_failure_returns_rc_2(tmp_path, capsys):
    def _resolver(host: str) -> list[str]:
        raise UnsafeUrlError(f"unresolvable test host: {host}")

    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: True,
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=_resolver,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    err = capsys.readouterr().err
    assert rc == 2
    assert "unsafe" in err.lower() or "refusing" in err.lower()


def test_add_no_auth_spec_skips_prompt_and_store(tmp_path):
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_NO_AUTH_SPEC),
        comprehend=lambda spec: 3,
        prompt=lambda q: calls.append(("prompt", q)) or "sk-live-x",
        store=lambda n, s: bool(calls.append(("store", n)) or True),
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    assert rc == 0
    assert calls == []


def test_add_empty_key_prints_add_later_still_rc_0(tmp_path, capsys):
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "",
        store=lambda n, s: bool(calls.append(("store", n)) or True),
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == []
    assert "add later" in out.lower()


def test_add_degraded_keychain_never_claims_sealed_still_rc_0(tmp_path, capsys):
    """Regression: a non-empty key whose store() reports failure (e.g. no OS
    keychain available) must never print '✓ sealed' — the secret was discarded."""
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: False,
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert "add later" in out.lower()
    assert "sealed" not in out.lower()


def test_add_claude_config_not_applied_prints_fallback_command_still_rc_0(
    tmp_path, capsys
):
    def _run(cmd: list[str]) -> int:
        raise FileNotFoundError("claude not found")

    deps = AddDeps(
        fetch=lambda u: json.dumps(_NO_AUTH_SPEC),
        comprehend=lambda spec: 3,
        prompt=lambda q: "unused",
        store=lambda n, s: True,
        run=_run,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", deps=deps)
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude mcp add" in out.lower() or "run the command" in out.lower()
    assert "gecko serve" in out


def test_add_slugifies_a_raw_name_consistently_with_remove(tmp_path):
    """Finding 4 regression: `gecko add --name "My API"` must produce the SAME
    slug `gecko rm "My API"` (or a raw `--name`-less `gecko add`) would look up —
    otherwise the cache file / credential slot / Claude registration desync."""
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_SPEC),
        comprehend=lambda spec: 47,
        prompt=lambda q: "sk-live-x",
        store=lambda n, s: bool(calls.append(("store", n)) or True),
        run=lambda cmd: bool(calls.append(("run", cmd))) or 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("https://api.stripe.com/openapi.json", name="My API", deps=deps)
    assert rc == 0
    expected_slug = safe_name("My API")
    assert expected_slug == "my-api"
    assert ("store", expected_slug) in calls
    assert (tmp_path / ".gecko" / "surfaces" / f"{expected_slug}.json").exists()
    run_cmd = next(cmd for kind, cmd in calls if kind == "run")
    assert expected_slug in run_cmd
    assert "--auth-keychain" in run_cmd and expected_slug in run_cmd


def test_add_local_path_needs_no_resolver(tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_NO_AUTH_SPEC))
    deps = AddDeps(
        fetch=lambda u: "",
        comprehend=lambda spec: 1,
        prompt=lambda q: "unused",
        store=lambda n, s: True,
        run=lambda cmd: 0,
        home=tmp_path,
    )
    rc = add(str(spec_path), deps=deps)
    assert rc == 0
