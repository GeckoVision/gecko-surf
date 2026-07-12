"""Tests for `gecko doctor` command."""

from gecko.cli import _cmd_doctor, main


def test_doctor_command_returns_zero(capsys):
    """gecko doctor should exit with rc 0."""
    rc = _cmd_doctor([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gecko" in out.lower()
    assert "surface" in out.lower()


def test_doctor_via_main_returns_zero(capsys):
    """gecko doctor via main() should exit with rc 0."""
    rc = main(["doctor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "gecko" in out.lower()


def test_doctor_help_flag(capsys):
    """gecko doctor --help should show help without crashing."""
    try:
        rc = _cmd_doctor(["--help"])
    except SystemExit as e:
        # argparse calls sys.exit(0) for --help
        rc = e.code
    # Either returns 0 or exits with 0
    assert rc == 0 or rc is None
    out = capsys.readouterr().out
    # Help text should mention the command
    assert "gecko" in out.lower() or "help" in out.lower()
