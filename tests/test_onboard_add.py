import json
import sys

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


_HOSTED_ELSEWHERE_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Colosseum", "version": "1"},
    # servers[] points at the API host, but the spec is fetched from ELSEWHERE
    # (GitHub raw) — the case pin_base_url can't trust on its own.
    "servers": [{"url": "https://copilot.colosseum.com/api/v1"}],
    "components": {"securitySchemes": {"b": {"type": "http", "scheme": "bearer"}}},
    "security": [{"b": []}],
    "paths": {},
}

_GH_AND_API = _fake_resolver(
    {
        "raw.githubusercontent.com": ["185.199.108.133"],
        "copilot.colosseum.com": ["104.18.0.1"],
    }
)


def test_add_explicit_base_url_pins_and_suppresses_mismatch_warning(tmp_path, capsys):
    """`gecko add <spec> --base-url <host>` — the one-line path for a spec whose own
    host doesn't serve it (Colosseum). The dev-asserted host OVERRIDES the fetch-origin
    pin, reaches the spawned `serve`, and suppresses the host-mismatch warning."""
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_HOSTED_ELSEWHERE_SPEC),
        comprehend=lambda spec: 11,
        prompt=lambda q: "pat-x",
        store=lambda n, s: bool(calls.append(("store", n)) or True),
        run=lambda cmd: bool(calls.append(("run", cmd))) or 0,
        home=tmp_path,
        resolver=_GH_AND_API,
    )
    rc = add(
        "https://raw.githubusercontent.com/GeckoVision/gecko-surf/main/spec.json",
        base_url="https://copilot.colosseum.com/api/v1",
        deps=deps,
    )
    captured = capsys.readouterr()
    assert rc == 0
    run_cmd = next(cmd for kind, cmd in calls if kind == "run")
    assert "--base-url" in run_cmd
    assert (
        run_cmd[run_cmd.index("--base-url") + 1]
        == "https://copilot.colosseum.com/api/v1"
    )
    # explicit assertion → no host-mismatch warning (the dev asserted the host)
    assert "differs from where the spec" not in captured.err


def test_add_mode_live_wires_live_surface(tmp_path):
    """`gecko add … --mode live` wires the served surface for real upstream calls
    (using the sealed key); recorded stays the default and omits the flag."""
    live_calls: list = []
    live = AddDeps(
        fetch=lambda u: json.dumps(_HOSTED_ELSEWHERE_SPEC),
        comprehend=lambda spec: 11,
        prompt=lambda q: "pat-x",
        store=lambda n, s: True,
        run=lambda cmd: bool(live_calls.append(cmd)) or 0,
        home=tmp_path,
        resolver=_GH_AND_API,
    )
    add(
        "https://raw.githubusercontent.com/x/spec.json",
        base_url="https://copilot.colosseum.com/api/v1",
        mode="live",
        deps=live,
    )
    assert "--mode" in live_calls[0]
    assert live_calls[0][live_calls[0].index("--mode") + 1] == "live"

    # default (recorded) omits the flag entirely — byte-identical to pre-flag wiring
    rec_calls: list = []
    rec = AddDeps(
        fetch=lambda u: json.dumps(_NO_AUTH_SPEC),
        comprehend=lambda spec: 3,
        prompt=lambda q: "unused",
        store=lambda n, s: True,
        run=lambda cmd: bool(rec_calls.append(cmd)) or 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    add("https://api.stripe.com/openapi.json", deps=rec)
    assert "--mode" not in rec_calls[0]


_MULTI_SERVER_SPEC = {
    "openapi": "3.0.3",
    "info": {"title": "Woovi-ish", "version": "1"},
    # Production FIRST — the money-API footgun order a silent servers[0] pick hits.
    "servers": [
        {"url": "https://api.woovi.example", "description": "Production"},
        {"url": "https://api.woovi-sandbox.example", "description": "Sandbox"},
    ],
    "paths": {},
}

_WOOVI_HOST = _fake_resolver({"api.woovi.example": ["93.184.216.34"]})


def test_add_mode_live_multi_server_without_base_url_refuses(tmp_path, capsys):
    """`gecko add --mode live` on a >1-server spec with no --base-url refuses BEFORE
    any wiring: non-zero exit, the server list + remediation on stderr, and neither
    the key prompt, the cache write, nor `claude mcp add` ever runs."""
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_MULTI_SERVER_SPEC),
        comprehend=lambda spec: 5,
        prompt=lambda q: calls.append(("prompt", q)) or "sk-live-x",
        store=lambda n, s: bool(calls.append(("store", n)) or True),
        run=lambda cmd: bool(calls.append(("run", cmd))) or 0,
        home=tmp_path,
        resolver=_WOOVI_HOST,
    )
    rc = add("https://api.woovi.example/openapi.json", mode="live", deps=deps)
    err = capsys.readouterr().err
    assert rc != 0
    assert "✗" in err
    assert "[0]" in err and "https://api.woovi.example" in err
    assert "[1]" in err and "https://api.woovi-sandbox.example" in err
    assert "--base-url" in err
    assert calls == []  # refused before key prompt / store / configure_claude
    assert not (tmp_path / ".gecko" / "surfaces").exists()


def test_add_mode_live_multi_server_with_base_url_proceeds(tmp_path):
    """The remediation works: the same spec + an explicit --base-url wires fine."""
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_MULTI_SERVER_SPEC),
        comprehend=lambda spec: 5,
        prompt=lambda q: "",
        store=lambda n, s: True,
        run=lambda cmd: bool(calls.append(cmd)) or 0,
        home=tmp_path,
        resolver=_fake_resolver(
            {
                "api.woovi.example": ["93.184.216.34"],
                "api.woovi-sandbox.example": ["93.184.216.35"],
            }
        ),
    )
    rc = add(
        "https://api.woovi.example/openapi.json",
        base_url="https://api.woovi-sandbox.example",
        mode="live",
        deps=deps,
    )
    assert rc == 0
    assert calls and "--base-url" in calls[0]


def test_add_recorded_multi_server_without_base_url_is_unchanged(tmp_path):
    """Recorded (the default, $0) never gains friction from the live-only guard."""
    calls = []
    deps = AddDeps(
        fetch=lambda u: json.dumps(_MULTI_SERVER_SPEC),
        comprehend=lambda spec: 5,
        prompt=lambda q: "",
        store=lambda n, s: True,
        run=lambda cmd: bool(calls.append(cmd)) or 0,
        home=tmp_path,
        resolver=_WOOVI_HOST,
    )
    rc = add("https://api.woovi.example/openapi.json", deps=deps)
    assert rc == 0
    assert calls  # wired as before


def test_add_rejects_unsafe_base_url(tmp_path, capsys):
    """An explicit --base-url is SSRF-validated like any other request target."""
    deps = AddDeps(
        fetch=lambda u: json.dumps(_HOSTED_ELSEWHERE_SPEC),
        comprehend=lambda spec: 11,
        prompt=lambda q: "pat-x",
        store=lambda n, s: True,
        run=lambda cmd: 0,
        home=tmp_path,
        resolver=_fake_resolver({"raw.githubusercontent.com": ["185.199.108.133"]}),
    )
    rc = add(
        "https://raw.githubusercontent.com/x/spec.json",
        base_url="http://169.254.169.254/latest/meta-data/",
        deps=deps,
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "unsafe" in err.lower() or "refusing" in err.lower()


def test_add_bare_domain_discovers_spec_and_wires(tmp_path):
    """The Pegana field repro: `gecko add api.stripe.com` (no scheme) must probe
    https://api.stripe.com through discovery — never be read as a local file."""
    bodies = {
        "https://api.stripe.com": "<html>landing</html>",
        "https://api.stripe.com/openapi.json": json.dumps(_NO_AUTH_SPEC),
    }

    def fetch(url: str) -> str:
        if url not in bodies:
            raise OSError(f"404 {url}")
        return bodies[url]

    calls: list = []
    deps = AddDeps(
        fetch=fetch,
        comprehend=lambda spec: 3,
        prompt=lambda q: "unused",
        store=lambda n, s: True,
        run=lambda cmd: bool(calls.append(cmd)) or 0,
        home=tmp_path,
        resolver=PUBLIC,
    )
    rc = add("api.stripe.com", deps=deps)
    assert rc == 0
    assert (tmp_path / ".gecko" / "surfaces" / "api-stripe-com.json").exists()
    # the wired serve is pinned to the DISCOVERED https origin, not a file path
    cmd = calls[0]
    assert "--base-url" in cmd
    assert cmd[cmd.index("--base-url") + 1] == "https://api.stripe.com"


def test_add_fallback_print_shows_npx_launcher_when_frozen_from_npx(
    tmp_path, capsys, monkeypatch
):
    """BUG C repro (npx world): when `gecko add` runs as the PyInstaller binary out
    of the npm/npx cache, the 'run the command above yourself' fallback must show a
    launcher that survives — `npx -y @geckovision/gecko serve …`, never a bare
    `gecko` that is not on the user's PATH."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/Users/raff/.npm/_npx/0123abc/node_modules/"
        "@geckovision/gecko-darwin-arm64/bin/gecko",
    )

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
    assert "npx -y @geckovision/gecko serve" in out
    assert "-- gecko serve" not in out  # the PATH-dependent spawn is gone


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
