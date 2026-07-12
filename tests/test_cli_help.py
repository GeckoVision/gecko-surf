"""Tests for gecko CLI banner and help output."""

from gecko.cli import _banner, _print_help


def test_banner_renders_plain_when_not_tty():
    """Under capture (not a TTY) the banner is the plain block wordmark — no color codes."""
    b = _banner()
    assert b and "\x1b[" not in b  # no ANSI escapes leak when piped/captured
    assert b.count("\n") >= 4  # multi-line block-letter wordmark


def test_help_groups_commands(capsys):
    """Help output must group commands and include key tokens."""
    _print_help()
    out = capsys.readouterr().out
    for token in ("add", "auth", "doctor", "Onboard", "make any API agent-usable"):
        assert token in out
