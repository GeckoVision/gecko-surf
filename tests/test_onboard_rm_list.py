"""Tests for gecko rm / gecko list (onboard.remove, onboard.list_surfaces)."""

from gecko.onboard import cache_spec, list_surfaces, remove


def test_list_and_remove(tmp_path):
    """list_surfaces and remove work end-to-end."""
    cache_spec("stripe", {"openapi": "3.0.3"}, home=tmp_path)
    assert "stripe" in list_surfaces(home=tmp_path)
    calls = []
    rc = remove("stripe", run=lambda cmd: calls.append(cmd) or 0, home=tmp_path)
    assert rc == 0
    assert not (tmp_path / ".gecko" / "surfaces" / "stripe.json").exists()
    assert calls and calls[0][:3] == ["claude", "mcp", "remove"]
    assert "stripe" not in list_surfaces(home=tmp_path)
