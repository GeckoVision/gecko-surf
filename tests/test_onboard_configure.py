import sys
from pathlib import Path

from gecko.onboard import _serve_launcher, configure_claude


def test_builds_stdio_add_command_and_applies():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return 0

    r = configure_claude("stripe", Path("/tmp/stripe.json"), gecko_bin="gecko", run=run)
    assert r.ok and r.applied
    assert calls[0][:5] == ["claude", "mcp", "add", "--transport", "stdio"]
    assert "--stdio" in calls[0] and "stripe" in calls[0]
    assert "/tmp/stripe.json" in calls[0]


def test_fallback_when_claude_missing_returns_command():
    def run(cmd):
        raise FileNotFoundError("claude")

    r = configure_claude("stripe", Path("/tmp/s.json"), run=run)
    assert not r.applied and r.ok  # ok=we produced a usable command
    assert r.command[0] == "claude"


def test_base_url_emits_flag_before_stdio():
    calls = []

    def run(cmd):
        calls.append(cmd)
        return 0

    r = configure_claude(
        "stripe",
        Path("/tmp/stripe.json"),
        run=run,
        base_url="https://api.stripe.com",
    )
    assert r.ok and r.applied
    cmd = calls[0]
    assert "--base-url" in cmd
    idx = cmd.index("--base-url")
    assert cmd[idx + 1] == "https://api.stripe.com"
    assert cmd.index("--base-url") < cmd.index("--stdio")


def test_no_base_url_omits_flag():
    r = configure_claude("stripe", Path("/tmp/stripe.json"), run=lambda cmd: 0)
    assert "--base-url" not in r.command


# --- the wired launcher must survive the invoker (pip / npx / frozen install) --------


def test_serve_launcher_not_frozen_is_bare_gecko(monkeypatch):
    # pip/uvx world: `gecko` IS a console script on PATH — keep current behavior.
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert _serve_launcher() == ["gecko"]


def test_serve_launcher_frozen_in_npx_cache_registers_npx(monkeypatch):
    # npx world: the binary runs from a prunable npm cache path — register via
    # `npx -y` so the client re-resolves the package on every spawn.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/Users/raff/.npm/_npx/0123abc/node_modules/"
        "@geckovision/gecko-darwin-arm64/bin/gecko",
    )
    assert _serve_launcher() == ["npx", "-y", "@geckovision/gecko"]


def test_serve_launcher_frozen_in_windows_npm_cache_registers_npx(monkeypatch):
    # The Windows npm cache spells it `npm-cache\_npx\…` — same prunable world.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        r"C:\Users\raff\AppData\Local\npm-cache\_npx\abc\node_modules"
        r"\@geckovision\gecko-win32-x64\bin\gecko.exe",
    )
    assert _serve_launcher() == ["npx", "-y", "@geckovision/gecko"]


def test_serve_launcher_frozen_stable_path_registers_absolute(monkeypatch):
    # A real (non-cache) frozen install: `gecko` may not be on the client's PATH —
    # register the absolute executable path.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/gecko")
    assert _serve_launcher() == ["/usr/local/bin/gecko"]


def test_configure_claude_registers_npx_launcher_when_frozen_from_npx(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys,
        "executable",
        "/home/raff/.npm/_npx/x/node_modules/@geckovision/gecko-linux-x64/bin/gecko",
    )
    calls = []
    r = configure_claude(
        "stripe", Path("/tmp/stripe.json"), run=lambda cmd: calls.append(cmd) or 0
    )
    assert r.ok and r.applied
    cmd = calls[0]
    sep = cmd.index("--")
    assert cmd[sep + 1 : sep + 5] == ["npx", "-y", "@geckovision/gecko", "serve"]
    assert "gecko" not in cmd  # no bare-PATH spawn survives the registration


def test_configure_claude_explicit_gecko_bin_still_wins(monkeypatch):
    # An explicit gecko_bin override beats the launcher heuristic (test seam +
    # power-user escape hatch) — backward compatible with existing callers.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/x/.npm/_npx/y/bin/gecko")
    r = configure_claude(
        "stripe", Path("/tmp/s.json"), gecko_bin="/opt/gecko", run=lambda cmd: 0
    )
    sep = r.command.index("--")
    assert r.command[sep + 1 : sep + 3] == ["/opt/gecko", "serve"]
