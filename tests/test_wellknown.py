"""Unit tests for the host-level ``.well-known`` breadcrumb helpers (pure, offline)."""

from __future__ import annotations

from gecko.wellknown import build_onboard_breadcrumb


def test_onboard_breadcrumb_carries_both_audiences_and_doc_links() -> None:
    md = build_onboard_breadcrumb("https://mcp.example.com")
    # Use-an-API path: the add-command + the quickstart canonical doc.
    assert "claude mcp add" in md
    assert "https://docs.geckovision.tech/quickstart" in md
    # Onboard-your-API path: the comprehend door + the providers canonical doc.
    assert "comprehend" in md
    assert "https://docs.geckovision.tech/for-providers" in md
    # It is a breadcrumb, not the full five-move depth — it points AT the docs.
    assert "canonical docs" in md.lower()


def test_onboard_breadcrumb_makes_paths_absolute_when_public_url_set() -> None:
    md = build_onboard_breadcrumb("https://mcp.example.com")
    assert "https://mcp.example.com/comprehend" in md
    assert "https://mcp.example.com/<name>/mcp" in md


def test_onboard_breadcrumb_relative_paths_without_public_url() -> None:
    md = build_onboard_breadcrumb(None)
    assert "/comprehend" in md
    assert "/<name>/mcp" in md
