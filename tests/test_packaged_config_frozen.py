"""Packaged-config reads must work inside a FROZEN binary, not just from source.

`gecko prove` shipped broken in 0.10.0 and every test passed. The reason: from source,
`importlib.resources.files(pkg)` returns a path whose `joinpath` accepts several segments;
inside a PyInstaller binary it returns a `MultiplexedPath`, whose `joinpath` accepts
exactly ONE. The multi-arg form therefore raised only in the packaged artifact — the one
place no test looked.

The fake below reproduces that constraint, so the difference is falsifiable from source.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gecko import provider_config


class _MultiplexedLike:
    """A stand-in for `MultiplexedPath`: `joinpath` takes exactly one segment.

    Faithful to the constraint that matters, and nothing else — a real MultiplexedPath
    would drag in the import machinery this test exists to avoid depending on.
    """

    def __init__(self, root: Any, parts: tuple[str, ...] = ()) -> None:
        self._root = root
        self._parts = parts

    def joinpath(self, segment: str) -> "_MultiplexedLike":  # NOTE: one segment only
        return _MultiplexedLike(self._root, (*self._parts, segment))

    def read_text(self, encoding: str = "utf-8") -> str:
        target = self._root
        for part in self._parts:
            target = target / part
        return target.read_text(encoding=encoding)


def test_packaged_config_reads_under_a_one_segment_joinpath(
    monkeypatch, tmp_path
) -> None:
    """The regression: a multi-arg joinpath raises `TypeError: takes 2 positional
    arguments but 3 were given` once frozen."""
    configs = tmp_path / "orquestra"
    configs.mkdir()
    (configs / "provider.json").write_text(
        json.dumps({"provider_id": "orquestra", "base_url": "https://x", "apis": []}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        provider_config.resources, "files", lambda _pkg: _MultiplexedLike(tmp_path)
    )

    data = provider_config._read_json("orquestra", "provider.json")

    assert data["provider_id"] == "orquestra"


def test_the_fake_actually_rejects_the_broken_call() -> None:
    """Guard the guard: if `_MultiplexedLike` ever accepted two segments, the test above
    would pass against the very bug it exists to catch."""
    anchor = _MultiplexedLike(None)

    with pytest.raises(TypeError):
        anchor.joinpath("orquestra", "provider.json")  # type: ignore[call-arg]


def test_every_packaged_provider_still_loads_from_source() -> None:
    """The behaviour the fix must not break."""
    provider, apis = provider_config.load_packaged_provider("orquestra")

    assert provider.provider_id == "orquestra"
    assert {"jupiter", "meteora", "pumpfun", "ore", "metadao_ico"} <= set(apis)
