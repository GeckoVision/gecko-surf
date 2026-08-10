"""The corpus's OWN untrusted-input boundary — the free-text exfiltration channel.

``error_class`` is a closed set and gated twice (``corpus.outcome_from`` and
``events.assert_fields_allowlisted``). The remaining free-text channels into the
persisted row were NOT gated at all:

* ``operation_id`` and ``path_template`` — UNTRUSTED-SPEC-derived. A poisoned spec
  picks the ``operationId`` and the path template, so it chooses what gets written
  into our store.
* ``params_present`` and the ``arg_shape`` KEYS — AGENT-supplied at call time. A
  compromised agent, not only a poisoned spec, chooses those strings.

Invariant #1 (control plane, never data plane) applies to our OWN corpus: an
unguarded field here is a secret-exfiltration pipe pointed at ourselves. These tests
are the gate — a violation is a build break, not a review comment.

The guard is ``looks_like_secret_value`` ONLY. The length cap and the base58
full-match that ``_guard_plan_name`` applies to PDA/plan names are deliberately NOT
applied here; ``test_calibration_*`` below is the empirical record of why (both
produce false positives on this repo's own real spec fixtures, and a false rejection
here silently drops a row out of the metric denominator).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from gecko import corpus
from gecko.client import AgentApiClient
from gecko.corpus import (
    DROPPED_ALLOWED_KEYS,
    DROPPED_FIELDS,
    DROPPED_REASONS,
    CorpusError,
    FreeTextGuardError,
    dropped_sibling,
    guard_freetext,
    outcome_from,
    record,
    recipe_hash,
    to_record,
)

# The proven defect payload: an 82-character AWS-access-key-shaped string. It is caught
# by ``looks_like_secret_value`` ALONE (no length cap, no base58 rule needed).
LEAKED_SECRET = "AKIAIOSFODNN7EXAMPLE-" + "K" * 61

CLEAN_INVOKE = {
    "method": "GET",
    "path": "/v1/assets/{asset_id}/state",
    "param_locations": {"asset_id": "path"},
}


def _outcome(**overrides: Any) -> corpus.CallOutcome:
    base: dict[str, Any] = dict(
        operation_id="get_asset_state",
        tool_invoke=CLEAN_INVOKE,
        args={"asset_id": "a1", "limit": 5},
        status=200,
        error_class="none",
        latency_ms=12,
        mode="live",
        auth_injected=True,
        ts=1_700_000_000_000,
        surface_id="pegana",
        surface_rev="rev-1",
    )
    base.update(overrides)
    return outcome_from(**base)


# --------------------------------------------------------------------------- #
# 1. The reproducing defect — one test per free-text channel.
# --------------------------------------------------------------------------- #
def test_secret_shaped_operation_id_is_refused() -> None:
    # A poisoned spec picks the operationId. Before the guard, this 82-char AKIA string
    # was persisted VERBATIM into the corpus row.
    with pytest.raises(FreeTextGuardError) as excinfo:
        _outcome(operation_id=LEAKED_SECRET)
    assert excinfo.value.field == "operation_id"
    # Redaction: the exception message names the FIELD, never echoes the value — an
    # exception message is a log line waiting to happen.
    assert LEAKED_SECRET not in str(excinfo.value)


def test_secret_shaped_path_template_is_refused() -> None:
    # A poisoned spec picks the path template too; this is the exact shape the live
    # probe reproduced (the key rode into path_template through tool_invoke["path"]).
    poisoned = dict(CLEAN_INVOKE, path=f"/v1/keys/{LEAKED_SECRET}/fetch")
    with pytest.raises(FreeTextGuardError) as excinfo:
        _outcome(tool_invoke=poisoned)
    assert excinfo.value.field == "path_template"
    assert LEAKED_SECRET not in str(excinfo.value)


def test_secret_shaped_arg_name_is_refused_for_both_derived_fields() -> None:
    # params_present and the arg_shape KEYS are the identical channel from the identical
    # source (the agent's own arg names) — one guard, both fields.
    with pytest.raises(FreeTextGuardError) as excinfo:
        _outcome(args={"asset_id": "a1", LEAKED_SECRET: 1})
    assert excinfo.value.field == "arg_names"
    assert LEAKED_SECRET not in str(excinfo.value)


def test_arg_names_channel_feeds_both_params_present_and_arg_shape() -> None:
    # Guard-once/derive-twice is only sound if BOTH fields really come from the guarded
    # name set. Pin that, so a future edit cannot re-open one of them.
    outcome = _outcome(args={"asset_id": "a1", "limit": 5, "body": {"x": 1}})
    assert outcome.params_present == ["asset_id", "limit"]
    assert set(outcome.arg_shape) == {"asset_id", "limit"}


def test_a_body_key_cannot_smuggle_a_secret_name_past_the_guard() -> None:
    # "body" is excluded from params_present/arg_shape, so it is not a persisted channel
    # and must not be guarded into a false rejection — but nothing about it is written.
    outcome = _outcome(args={"body": {"secret": LEAKED_SECRET}})
    assert outcome.body_present is True
    assert LEAKED_SECRET not in json.dumps(to_record(outcome))


@pytest.mark.parametrize(
    "secret",
    [
        LEAKED_SECRET,
        "sk-" + "A" * 32,  # OpenAI-style
        "sk_live_" + "B" * 24,  # Stripe-style
        "ghp_" + "C" * 36,  # GitHub PAT
        "-----BEGIN RSA PRIVATE KEY-----",
        "1" * 64,  # raw 32-byte hex
        "5" + "K" * 87,  # base58 at SOLANA SECRET-KEY length (~88)
    ],
)
def test_every_secret_family_is_refused_on_every_guarded_field(secret: str) -> None:
    with pytest.raises(FreeTextGuardError):
        _outcome(operation_id=secret)
    with pytest.raises(FreeTextGuardError):
        _outcome(tool_invoke=dict(CLEAN_INVOKE, path=f"/v1/{secret}"))
    with pytest.raises(FreeTextGuardError):
        _outcome(args={secret: 1})


def test_the_read_path_refuses_a_pre_guard_poisoned_row() -> None:
    """Both boundaries, same doctrine as ``tenancy``. Rows written BEFORE this guard
    existed (or a hand-supplied fixture) can still carry a secret-shaped name, and
    rehydrating one would re-admit the value into memory and into whatever the reader
    does next."""
    clean = to_record(_outcome())
    for field, poisoned in (
        ("operation_id", dict(clean, operation_id=LEAKED_SECRET)),
        ("path_template", dict(clean, path_template=f"/v1/{LEAKED_SECRET}")),
        ("params_present", dict(clean, params_present=[LEAKED_SECRET])),
        ("arg_shape", dict(clean, arg_shape={LEAKED_SECRET: "string"})),
    ):
        with pytest.raises(FreeTextGuardError):
            corpus.outcome_from_record(poisoned), field
    # ...and the clean row still rehydrates unchanged.
    assert corpus.outcome_from_record(clean).operation_id == "get_asset_state"


# --------------------------------------------------------------------------- #
# 2. FAIL CLOSED means REFUSE, never truncate.
# --------------------------------------------------------------------------- #
def test_the_row_is_refused_not_truncated(tmp_path: Path) -> None:
    """A truncated secret is still a secret, and a truncated row silently enters the
    denominator. Neither the whole value nor any prefix of it may reach the file, and no
    partial row may be written."""
    corpus_path = tmp_path / "corpus.jsonl"
    with pytest.raises(CorpusError):
        record(_outcome(operation_id=LEAKED_SECRET), corpus_path)  # type: ignore[arg-type]
    assert not corpus_path.exists()
    for other in tmp_path.iterdir():
        text = other.read_text()
        # No prefix survives either — "capped at 64" would still leak the AKIA id.
        for cut in range(8, len(LEAKED_SECRET) + 1):
            assert LEAKED_SECRET[:cut] not in text


def test_guard_freetext_returns_the_value_unchanged_when_clean() -> None:
    # The guard is a gate, not a transformer: a clean value must survive byte-identical.
    assert guard_freetext("get_asset_state", "operation_id") == "get_asset_state"
    long_path = "/v1/condition_sets/{condition_set_id}/condition_set_items/{item_id}"
    assert guard_freetext(long_path, "path_template") == long_path


def test_a_non_string_field_is_refused() -> None:
    # A spec may set operationId to a non-string; a dict/list here would serialize a
    # structure into a name slot.
    with pytest.raises(CorpusError):
        guard_freetext({"a": 1}, "operation_id")
    with pytest.raises(CorpusError):
        guard_freetext(None, "path_template")


# --------------------------------------------------------------------------- #
# 3. The known base58 false-positive class must NOT be reintroduced.
# --------------------------------------------------------------------------- #
def test_a_base58_shaped_operation_id_is_admitted() -> None:
    """STATE.md records a base58 false positive that quarantines legitimate surfaces.
    ``getApiFixturesUpdatesEpochdayHourofday`` is a REAL operationId in this repo's own
    TxLINE fixture and is a full base58 match at 38 chars — ``_guard_plan_name`` would
    reject it. The corpus guard must not, or a real API's rows vanish from the metric."""
    op = "getApiFixturesUpdatesEpochdayHourofday"
    assert guard_freetext(op, "operation_id") == op
    assert _outcome(operation_id=op).operation_id == op


def test_the_long_real_path_template_is_admitted() -> None:
    """The 81-char Pegana path is a REAL fixture path. A 64-char cap rejects it — which
    is why no length cap ships on these fields."""
    path = "/v1/condition_sets/{condition_set_id}/condition_set_items/{condition_set_item_id}"
    assert len(path) == 81
    assert _outcome(tool_invoke=dict(CLEAN_INVOKE, path=path)).path_template == path


def test_guard_plan_name_keeps_its_stricter_rules_for_pda_names() -> None:
    """The relaxation is scoped to the corpus free-text fields. ``recipe_hash``'s plan
    names keep the length cap AND the base58 full-match — there a base58 string really is
    a resolved pubkey, and the fingerprint must stay values-free."""
    resolved_pubkey = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
    with pytest.raises(CorpusError):
        recipe_hash(
            program_id="P",
            instruction="buy",
            account_names=[resolved_pubkey],
            arg_names=["amount"],
            seed_recipes={},
        )
    with pytest.raises(CorpusError):
        recipe_hash(
            program_id="P",
            instruction="buy",
            account_names=["a" * 65],
            arg_names=["amount"],
            seed_recipes={},
        )


# --------------------------------------------------------------------------- #
# 4. Calibration — the empirical record, committed as the test.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parent.parent
# ``private/`` is gitignored (absent on a fresh clone); everything else that ships is in.
_SPEC_ROOTS = ("tests/fixtures", "examples", "gecko/examples", "docs")


def _load_doc(path: Path) -> Any:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(text)
    except (ValueError, UnicodeDecodeError):
        try:
            # Some fixtures are JSON-with-trailing-commas or real YAML.
            return yaml.safe_load(text)
        except yaml.YAMLError:
            return None


def _spec_strings() -> tuple[list[str], list[str], list[str]]:
    """Every operationId, path template and param NAME in every spec that ships."""
    op_ids: list[str] = []
    paths: list[str] = []
    params: list[str] = []
    for root in _SPEC_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for spec_file in sorted(base.rglob("*")):
            if spec_file.suffix not in (".json", ".yaml", ".yml"):
                continue
            doc = _load_doc(spec_file)
            if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
                continue
            for path, item in doc["paths"].items():
                if not isinstance(path, str):
                    continue
                paths.append(path)
                if not isinstance(item, dict):
                    continue
                for operation in item.values():
                    if not isinstance(operation, dict):
                        continue
                    op_id = operation.get("operationId")
                    if isinstance(op_id, str):
                        op_ids.append(op_id)
                    for param in operation.get("parameters") or []:
                        if isinstance(param, dict) and isinstance(
                            param.get("name"), str
                        ):
                            params.append(param["name"])
    return op_ids, paths, params


def test_calibration_the_shipped_guard_rejects_nothing_real() -> None:
    """The empirical proof, run against EVERY spec fixture in the repo: the shipped
    guard (``looks_like_secret_value`` alone) has ZERO false positives. A guard that
    drops legitimate rows is a silent denominator hole, so this is the gate on any
    future tightening — tighten the rule, re-run this, and it must stay at zero."""
    op_ids, paths, params = _spec_strings()
    # Guard against the sweep silently finding nothing (a moved fixture dir).
    assert len(op_ids) > 300 and len(paths) > 300 and len(params) > 100
    rejected = [
        (field, value)
        for field, values in (
            ("operation_id", op_ids),
            ("path_template", paths),
            ("arg_names", params),
        )
        for value in values
        if _rejects(value, field)
    ]
    assert rejected == []


def _rejects(value: str, field: str) -> bool:
    try:
        guard_freetext(value, field)
    except CorpusError:
        return True
    return False


def test_calibration_records_why_the_stricter_rules_were_dropped() -> None:
    """The counter-evidence, pinned so it cannot be re-argued from memory: applying
    ``_guard_plan_name``'s length cap and base58 full-match to these fields DOES reject
    real fixtures. This is the reason those two rules are not on the corpus fields."""
    from gecko.corpus import _MAX_PLAN_NAME, _BASE58_ADDRESS_RE

    op_ids, paths, params = _spec_strings()
    over_cap = [v for v in op_ids + paths + params if len(v) > _MAX_PLAN_NAME]
    base58_fp = [v for v in op_ids + paths + params if _BASE58_ADDRESS_RE.fullmatch(v)]
    assert over_cap, "a 64-char cap must be shown to reject a real fixture"
    assert base58_fp, "the base58 full-match must be shown to reject a real fixture"
    # And every one of those false positives is admitted by the shipped guard.
    for value in over_cap + base58_fp:
        assert guard_freetext(value, "operation_id") == value


# --------------------------------------------------------------------------- #
# 5. Fail closed must not mean "break the live call".
# --------------------------------------------------------------------------- #
def _poisoned_spec(path: str) -> dict[str, Any]:
    return {
        "openapi": "3.0.0",
        "info": {"title": "poisoned", "version": "1"},
        "servers": [{"url": "https://api.example.com"}],
        "paths": {
            path: {
                "get": {
                    "operationId": "fetch_key",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {"schema": {"type": "object"}}
                            },
                        }
                    },
                }
            }
        },
    }


def test_a_guard_violation_on_a_live_call_still_returns_the_response(
    tmp_path: Path,
) -> None:
    """``corpus.record`` re-raises ``CorpusError`` and ``client._capture`` sits OUTSIDE
    the ``try/except CallError`` — so an unwrapped guard would send the request, receive
    the response, and THEN destroy the agent's result, after the side effect. Fail closed
    means REFUSE TO PERSIST, never propagate into ``client.call``."""
    corpus_path = tmp_path / "corpus.jsonl"
    client = AgentApiClient(
        _poisoned_spec(f"/v1/keys/{LEAKED_SECRET}/fetch"),
        base_url="https://api.example.com",
        corpus_path=corpus_path,
        surface_id="poisoned",
        live_transport=lambda req: (200, {"ok": True}),
    )
    result = client.call("fetch_key", {}, mode="live")

    # The agent still gets its result — the call happened, the side effect is real.
    assert result["status"] == 200
    assert result["mode"] == "live"
    assert result["data"] == {"ok": True}

    # ...and nothing leaked into any corpus file.
    for written in tmp_path.rglob("*.jsonl"):
        assert LEAKED_SECRET not in written.read_text()


def test_a_dropped_row_is_counted_not_silent(tmp_path: Path) -> None:
    """A refused row leaves a hole in the denominator. The hole must be COUNTABLE — a
    categorical drop record in a segregated sibling, carrying the field and the reason
    and never the offending value."""
    corpus_path = tmp_path / "corpus.jsonl"
    client = AgentApiClient(
        _poisoned_spec(f"/v1/keys/{LEAKED_SECRET}/fetch"),
        base_url="https://api.example.com",
        corpus_path=corpus_path,
        surface_id="poisoned",
        live_transport=lambda req: (200, {"ok": True}),
    )
    client.call("fetch_key", {}, mode="live")

    dropped_path = dropped_sibling(corpus_path)
    rows = [json.loads(line) for line in dropped_path.read_text().splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    assert set(row) <= DROPPED_ALLOWED_KEYS
    assert row["field"] == "path_template"
    assert row["reason"] == "secret_shaped"
    assert row["source"] == "observed"  # which bucket lost a row
    assert row["surface_id"] == "poisoned"
    assert LEAKED_SECRET not in dropped_path.read_text()
    # The main corpus stays clean — a dropped row is NOT a written row.
    assert not corpus_path.exists()


def test_drop_record_vocabularies_are_closed() -> None:
    assert DROPPED_REASONS == frozenset({"secret_shaped", "control_plane_violation"})
    assert DROPPED_FIELDS == frozenset(
        {"operation_id", "path_template", "arg_names", "other"}
    )
    with pytest.raises(CorpusError):
        corpus.record_dropped(
            corpus.DroppedRow(
                ts=1,
                surface_id="s",
                field="operation_id",
                reason="because-i-said-so",
                source="observed",
            ),
            "/dev/null",
        )
    with pytest.raises(CorpusError):
        corpus.record_dropped(
            corpus.DroppedRow(
                ts=1,
                surface_id="s",
                field="response_body",
                reason="secret_shaped",
                source="observed",
            ),
            "/dev/null",
        )


def test_a_secret_shaped_surface_id_is_redacted_on_the_drop_row(
    tmp_path: Path,
) -> None:
    """The drop record must not become the leak it exists to report. ``surface_id`` is
    operator-supplied, so it gets the same predicate — and a categorical fallback rather
    than a rejection (refusing the DROP record would restore the silent hole)."""
    corpus_path = tmp_path / "corpus.jsonl"
    corpus.record_dropped(
        corpus.DroppedRow(
            ts=1,
            surface_id=LEAKED_SECRET,
            field="operation_id",
            reason="secret_shaped",
            source="observed",
        ),
        corpus_path,
    )
    text = dropped_sibling(corpus_path).read_text()
    assert LEAKED_SECRET not in text
    assert json.loads(text.strip())["surface_id"] == "redacted"


def test_every_persisting_call_site_goes_through_the_wrapped_boundary() -> None:
    """ "No caller must remember" — enforced structurally. Every site that PERSISTS a
    ``CallOutcome`` must go through ``capture.record_outcome``; a raw
    ``corpus.record(corpus.outcome_from(...))`` would fail open again (it would raise into
    whatever call path it sits on). ``verify.py`` is the one allowed direct user: it BUILDS
    an outcome for comparison and never writes it, so a violation there should raise."""
    package = _REPO_ROOT / "gecko"
    offenders = []
    for module in sorted(package.rglob("*.py")):
        if module.name in ("corpus.py", "capture.py", "verify.py"):
            continue
        text = module.read_text(encoding="utf-8")
        if "outcome_from(" in text.replace("simulated_outcome_from(", ""):
            if "record_outcome(" not in text:
                offenders.append(module.relative_to(_REPO_ROOT).as_posix())
    assert offenders == []


def test_a_recorded_mode_violation_also_returns_the_synthesized_response(
    tmp_path: Path,
) -> None:
    # Same wrapper, the $0 path — the recorded demo must not blow up on a poisoned spec.
    corpus_path = tmp_path / "corpus.jsonl"
    client = AgentApiClient(
        _poisoned_spec(f"/v1/keys/{LEAKED_SECRET}/fetch"),
        base_url="https://api.example.com",
        corpus_path=corpus_path,
        surface_id="poisoned",
    )
    result = client.call("fetch_key", {}, mode="recorded")
    assert result["status"] == 200
    rows = [
        json.loads(line)
        for line in dropped_sibling(corpus_path).read_text().splitlines()
        if line
    ]
    assert rows[0]["source"] == "synthetic"  # the synthetic bucket lost the row
