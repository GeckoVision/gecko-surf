"""``anyquery_deps_from_env``: the engine builds no hosted deps; a host injects them."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest

from gecko.anyquery.hosted import NotServedError, anyquery_deps_from_env
from gecko.credentials import PLATFORM_UNSET_SENTINEL


def test_with_no_builder_there_is_nothing_to_serve() -> None:
    assert anyquery_deps_from_env({"GECKO_ANYQUERY_BILLING": "solana"}) is None


def test_the_builder_sees_the_environment_without_sentinels() -> None:
    seen: dict[str, str] = {}
    deps: Any = object()

    def build(env: Mapping[str, str]) -> Any:
        seen.update(env)
        return deps

    got = anyquery_deps_from_env(
        {"KEEP": "x", "DROP": PLATFORM_UNSET_SENTINEL}, build=build
    )

    assert got is deps
    assert seen == {"KEEP": "x"}


def test_a_builder_that_refuses_is_no_mount_with_the_reason_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def build(env: Mapping[str, str]) -> Any:
        raise NotServedError("GECKO_ANYQUERY_RPC_URL is not set")

    with caplog.at_level(logging.WARNING, logger="gecko.anyquery.hosted"):
        assert anyquery_deps_from_env({"SECRET": "v4lue"}, build=build) is None

    assert "GECKO_ANYQUERY_RPC_URL is not set" in caplog.text
    assert "v4lue" not in caplog.text
