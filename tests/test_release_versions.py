"""Every version marker in the repo must equal `pyproject.toml`'s `version`.

WHY THIS EXISTS, and it is a shipped defect rather than tidiness. npm
`@geckovision/gecko-linux-x64@0.11.0` contains a binary that prints `gecko 0.10.3`.
The tag `v0.11.0` was pushed at a commit whose `pyproject.toml` still said `0.10.3`
(`1e726d3`), the release build there succeeded, and `publish-npm` shipped it under the
tag's number; the tag was then moved to the real bump (`e374f7c`) but npm versions are
immutable, so that package is wrong forever. `release.yaml` now gates on the frozen
binary's own `--version`, which stops a mismatched build from ever being uploaded or
published. THIS test is the other half: it fails on the PR, before a tag exists, when a
release bump missed a marker — which is how the two numbers came apart in the first
place.

It also catches the drift RELEASING.md was written for: 0.3.0 shipped with the plugin
manifests still reading 0.2.3, so a teammate who refreshed the plugin saw no version
change and could not tell whether it had updated. At the time of writing every marker
below had drifted at least two releases behind `pyproject`.

`examples/agent-plugin/**` is deliberately NOT here: those manifests version an example
provider's plugin, not this package, and must not track our releases.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]

# Mirrors the marker table in RELEASING.md — keep the two in step.
_PLUGIN_MANIFESTS = (
    "plugin.json",  # the Agent Plugins 1.0.0 manifest at the repo root
    "skills/.claude-plugin/plugin.json",
    "skills/.cursor-plugin/plugin.json",
)
# Marketplace listings carry the version one level down, per plugin entry.
_MARKETPLACES = (
    ".claude-plugin/marketplace.json",
    ".cursor-plugin/marketplace.json",
)
_NPM_LAUNCHER = "npm/gecko/package.json"


def _package_version() -> str:
    with (_REPO / "pyproject.toml").open("rb") as fh:
        version: str = tomllib.load(fh)["project"]["version"]
    return version


@pytest.mark.parametrize("relpath", _PLUGIN_MANIFESTS)
def test_plugin_manifest_tracks_pyproject(relpath: str) -> None:
    manifest = json.loads((_REPO / relpath).read_text())
    assert manifest["version"] == _package_version(), (
        f"{relpath} says {manifest['version']!r}, pyproject says "
        f"{_package_version()!r} — bump every marker in RELEASING.md together"
    )


@pytest.mark.parametrize("relpath", _MARKETPLACES)
def test_marketplace_listing_tracks_pyproject(relpath: str) -> None:
    listing = json.loads((_REPO / relpath).read_text())
    for plugin in listing["plugins"]:
        assert plugin["version"] == _package_version(), (
            f"{relpath} lists {plugin['name']} at {plugin['version']!r}, pyproject "
            f"says {_package_version()!r} — a stale listing is how a user reinstalls "
            f"and cannot tell whether anything changed"
        )


def test_npm_launcher_version_tracks_pyproject() -> None:
    pkg = json.loads((_REPO / _NPM_LAUNCHER).read_text())
    assert pkg["version"] == _package_version()


def test_npm_launcher_pins_its_platform_packages_to_the_same_version() -> None:
    """The launcher resolves the host's binary through `optionalDependencies`, each
    pinned `=X.Y.Z`. CI re-stamps these from the tag at publish time, so a stale value
    cannot reach npm through `release.yaml` — but `npm/scripts/bootstrap-publish.sh`
    and any hand publish read the committed file, and a pin left at `=0.9.5` (which is
    what sat in the tree while 0.11.0 was current) would install a two-release-old
    binary under a current launcher."""
    pkg = json.loads((_REPO / _NPM_LAUNCHER).read_text())
    expected = f"={_package_version()}"
    for name, pin in pkg["optionalDependencies"].items():
        assert pin == expected, (
            f"{_NPM_LAUNCHER}: {name} is pinned {pin!r}, expected {expected!r}"
        )
