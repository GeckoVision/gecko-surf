"""Drift check for the VENDORED Arazzo schema — we sell drift detection; run it on us.

`tests/fixtures/arazzo/arazzo-1.0-schema.json` is the official OAI schema, vendored so
the conformance test in `test_arazzo.py` runs offline. Vendoring buys determinism and
costs freshness: the copy can silently diverge from upstream, and nothing told us.

Two checks, deliberately split by what they can prove:

* **The pin** (offline, always runs). The file's sha256 must equal the value recorded in
  `tests/fixtures/arazzo/README.md`. This catches a LOCAL edit — someone "fixing" the
  schema to make a failing conformance test pass, which would turn our only real
  conformance signal into a mirror. It cannot detect upstream movement.
* **Upstream** (network, opt-in via ``GECKO_CHECK_UPSTREAM_SCHEMA=1``). Fetches the
  source and compares. This is the actual drift detector. It is opt-in because a CI
  job that fails when a third party's repo reorganises is a flaky job, and a flaky job
  gets muted — the same reasoning as `--allow-missing-channels` in Skill Guard.

The upstream path is itself a drift signal: the schema already lives under `_archive_/`,
so a 404 means the file moved, which is exactly the thing worth being told about. The
check reports that as drift rather than as a test error.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "arazzo"
SCHEMA = FIX / "arazzo-1.0-schema.json"
README = FIX / "README.md"

#: Where the vendored copy came from. Raw content, pinned to the default branch — the
#: same ref the README records as its source.
UPSTREAM_URL = (
    "https://raw.githubusercontent.com/OAI/Arazzo-Specification/main/"
    "_archive_/schemas/v1.0/schema.json"
)

#: The `arazzo` version string our exporter emits must satisfy the schema's own pattern.
#: Pinned here so bumping ARAZZO_VERSION past what the vendored schema accepts fails
#: loudly rather than producing documents a conformant runtime rejects.
_VERSION_FIELD = ("properties", "arazzo", "pattern")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pinned_sha() -> str:
    """The sha256 the README records. Parsed, not duplicated — one source of truth."""
    match = re.search(r"`([0-9a-f]{64})`", README.read_text())
    assert match, "tests/fixtures/arazzo/README.md must record the schema's sha256"
    return match.group(1)


def test_vendored_schema_matches_its_recorded_pin() -> None:
    """The vendored file is byte-identical to what the README says it is.

    A conformance test that validates against a locally-edited schema proves nothing —
    it checks our output against our own opinion. This is the guard that keeps
    `test_arazzo.py` honest.
    """
    assert _sha256(SCHEMA) == _pinned_sha(), (
        "the vendored Arazzo schema no longer matches the sha256 in its README. "
        "Either it was edited locally (don't — the conformance test stops meaning "
        "anything) or it was deliberately re-vendored, in which case update the "
        "README's hash, retrieval date and $id in the same commit."
    )


def test_vendored_schema_accepts_the_version_we_emit() -> None:
    """Our emitted `arazzo` version must satisfy the vendored schema's own pattern.

    We emit 1.0.1 while every official example is 1.0.0; both are legal under
    `^1\\.0\\.\\d+(-.+)?$`. This pins that relationship so a future bump to, say, 1.1.0
    fails here — next to the schema that would reject it — instead of downstream in a
    runtime we do not control.
    """
    from gecko.arazzo import ARAZZO_VERSION

    schema = json.loads(SCHEMA.read_text())
    node: object = schema
    for key in _VERSION_FIELD:
        assert isinstance(node, dict)
        node = node[key]
    assert isinstance(node, str)
    assert re.match(node, ARAZZO_VERSION), (
        f"gecko.arazzo.ARAZZO_VERSION is {ARAZZO_VERSION!r}, which the vendored schema's "
        f"pattern {node!r} rejects. Re-vendor the schema for that version first — "
        "emitting a version the schema we validate against cannot accept means our "
        "conformance test is no longer testing conformance."
    )


@pytest.mark.skipif(
    os.environ.get("GECKO_CHECK_UPSTREAM_SCHEMA") != "1",
    reason="network drift check — set GECKO_CHECK_UPSTREAM_SCHEMA=1 to run",
)
def test_vendored_schema_has_not_drifted_from_upstream() -> None:
    """The real drift detector: fetch upstream and compare.

    Opt-in and network-bound. A 404 is reported as drift (the file moved — that is the
    news), never as an inconclusive pass: a check that cannot reach its source must say
    so rather than report agreement it did not verify.
    """
    from gecko.netguard import safe_get

    try:
        upstream = safe_get(UPSTREAM_URL)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        pytest.fail(
            f"upstream schema is no longer at {UPSTREAM_URL} (HTTP {exc.code}). "
            "That IS the drift signal — the OAI repo moved or renamed it. Find the new "
            "location, re-vendor, and update the README's source row."
        )
    except (urllib.error.URLError, OSError) as exc:  # pragma: no cover - network
        pytest.skip(f"upstream unreachable, no verdict claimed: {exc}")

    local_hash = hashlib.sha256(SCHEMA.read_bytes()).hexdigest()
    upstream_hash = hashlib.sha256(upstream.encode()).hexdigest()
    if local_hash == upstream_hash:
        return

    # Not necessarily an error — upstream may have reformatted without changing meaning.
    # Say which it is, because the two need different responses.
    local_doc = json.loads(SCHEMA.read_text())
    upstream_doc = json.loads(upstream)
    semantic = local_doc != upstream_doc
    pytest.fail(
        f"vendored Arazzo schema has drifted from {UPSTREAM_URL}\n"
        f"  local    sha256 {local_hash}\n"
        f"  upstream sha256 {upstream_hash}\n"
        f"  semantic change: {semantic}\n"
        + (
            "  the parsed documents DIFFER — re-vendor and re-run the conformance "
            "suite; our exporter may now emit documents the current schema rejects."
            if semantic
            else "  the parsed documents are EQUAL — upstream only reformatted. "
            "Re-vendor to silence this, no conformance risk."
        )
    )
