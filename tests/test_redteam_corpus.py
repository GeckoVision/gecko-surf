"""Control-plane gate for the red-team (adversarial) corpus additions.

Mirrors ``tests/test_corpus_controlplane.py``: an ``AdversarialOutcome`` may persist
ONLY categorical/bool metadata — never a canary, host, address, amount, or any arg
value. Two structural guarantees back that promise: the field set IS the schema
(``ADVERSARIAL_ALLOWED_KEYS``), and the writer fails closed on any non-allowlisted key
or off-closed-set categorical value.
"""

from __future__ import annotations

import json

import pytest

from gecko.corpus import (
    ADVERSARIAL_ALLOWED_KEYS,
    BLOCKED_REASONS,
    FAMILIES,
    VERDICTS,
    AdversarialOutcome,
    CorpusError,
    assert_adversarial_allowlisted,
    record_adversarial,
    to_adversarial_record,
)

# Secret-shaped strings the harness handles are canaries only; none may reach the file.
_CANARIES = ("CANARY_JWT_7f3a", "CANARY_TOK_9b2c", "CANARY_PK_4d1e")
_ATTACKER_ADDR = "0xdeaddeaddeaddeaddeaddeaddeaddeaddeaddead"
_U64_MAX = "18446744073709551615"


def _make_adv(**overrides) -> AdversarialOutcome:
    base = dict(
        ts=1_700_000_000_000,
        scenario_id="exfil-key-in-desc",
        family="A",
        tier=0,
        layer="L1",
        vector="description",
        polarity="attack",
        policy_id="scripted-defended",
        defenses=["sanitizer", "auth_firewall", "quarantine"],
        verdict="blocked",
        tripped_predicate="canary_in_request",
        blocked_reason="instruction_stripped",
        leaked=False,
        leak_sink=None,
        auth_host_ok=True,
    )
    base.update(overrides)
    return AdversarialOutcome(**base)


def test_blocked_reasons_is_frozenset_and_closed():
    assert isinstance(BLOCKED_REASONS, frozenset)
    assert "none" in BLOCKED_REASONS
    # The reasons the enforce points can raise are all present.
    for reason in (
        "instruction_stripped",
        "secret_value_dropped",
        "address_value_dropped",
        "surface_quarantined",
        "auth_host_blocked",
        "payment_reqs_untrusted",
    ):
        assert reason in BLOCKED_REASONS


def test_adversarial_outcome_allowlist_matches_fields():
    # The allowlist IS the schema — every persisted key is a declared field.
    assert ADVERSARIAL_ALLOWED_KEYS == set(AdversarialOutcome.__dataclass_fields__)
    record = to_adversarial_record(_make_adv())
    assert set(record) == ADVERSARIAL_ALLOWED_KEYS


def test_to_adversarial_record_rejects_non_allowlisted_key():
    tampered = to_adversarial_record(_make_adv())
    tampered["leaked_value"] = _CANARIES[0]  # a secret sneaking in as a new key
    with pytest.raises(CorpusError):
        assert_adversarial_allowlisted(tampered)


def test_to_adversarial_record_rejects_bad_family_verdict_reason():
    # Fail closed on any off-closed-set categorical value, like outcome_from does.
    with pytest.raises(CorpusError):
        to_adversarial_record(_make_adv(family="Z"))
    with pytest.raises(CorpusError):
        to_adversarial_record(_make_adv(verdict="pwned"))
    with pytest.raises(CorpusError):
        to_adversarial_record(_make_adv(blocked_reason="whatever"))
    # sanity: the closed sets themselves are the source of truth
    assert "A" in FAMILIES and "exploited" in VERDICTS


def test_to_adversarial_record_validates_leak_sink_shape():
    # leak_sink is the ONE field built from request data (a header NAME), so it is closed-
    # set-guarded too: only url/body/header:<safe-name> may persist, fail closed otherwise.
    for ok in (None, "url", "body", "header:X-Api-Token"):
        to_adversarial_record(_make_adv(leaked=bool(ok), leak_sink=ok))
    with pytest.raises(CorpusError):
        to_adversarial_record(_make_adv(leaked=True, leak_sink="cookie:session"))
    with pytest.raises(CorpusError):
        # a secret-shaped header NAME (a poisoned spec could declare one) must not reach disk
        to_adversarial_record(
            _make_adv(leaked=True, leak_sink="header:sk-ABCDEFGHIJKLMNOPQRSTUVWX")
        )


def test_record_adversarial_writes_no_canary_or_value(tmp_path):
    # The killer test: even the "leaked=True" record names a channel, never a value.
    path = tmp_path / "adversarial.jsonl"
    record_adversarial(
        _make_adv(
            scenario_id="exfil-auth-redirect",
            layer="L2",
            vector="servers_url",
            verdict="exploited",
            blocked_reason="none",
            leaked=True,
            leak_sink="header:X-Api-Token",
            auth_host_ok=False,
        ),
        path,
    )
    raw = path.read_text()
    line = json.loads(raw.strip())
    assert set(line) == ADVERSARIAL_ALLOWED_KEYS
    for secret in (*_CANARIES, _ATTACKER_ADDR, _U64_MAX, "router"):
        assert secret not in raw
    assert line["leak_sink"] == "header:X-Api-Token"  # channel NAME only
