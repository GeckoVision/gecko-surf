from pathlib import Path

from gecko.onboard import configure_claude


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
