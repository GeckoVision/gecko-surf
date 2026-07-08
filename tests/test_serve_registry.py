"""gecko serve --registry: fetch from the registry instead of a spec path."""

import pytest

from gecko import serve


def test_registry_and_spec_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        serve._parse_args(["./spec.json", "--registry", "colosseum"])


def test_registry_flag_parses():
    args = serve._parse_args(
        ["--registry", "colosseum", "--auth-env", "COLOSSEUM_COPILOT_PAT"]
    )
    assert args.registry == "colosseum"
    assert args.registry_url == "https://mcp.geckovision.tech"
    assert args.auth_env == "COLOSSEUM_COPILOT_PAT"
    assert args.spec is None


def test_spec_still_works_without_registry():
    args = serve._parse_args(["./spec.json"])
    assert args.spec == "./spec.json" and args.registry is None
