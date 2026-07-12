"""Tests for gecko CLI banner and help output."""

from gecko.cli import _banner, _print_help


def test_banner_contains_wordmark():
    """Banner output must contain the GECKO wordmark."""
    assert "GECKO" in _banner().upper()


def test_help_groups_commands(capsys):
    """Help output must group commands and include key tokens."""
    _print_help()
    out = capsys.readouterr().out
    for token in ("add", "auth", "doctor", "Onboard", "make any API agent-usable"):
        assert token in out
