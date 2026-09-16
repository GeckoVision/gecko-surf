"""The Kora relay client, offline: what it sends, what it refuses, what it never says."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gecko.relay import FeePayerRelay  # noqa: E402
from scripts.kora_relay import KoraRelay, KoraRelayError, KoraRequest  # noqa: E402

RELAY = "CfR1NAWHt3p5zNz8s3nPz2k6UqU2vD8G4H6pVdJvsg5Q"
SECRET = "kora-key-that-must-never-print"


class _Transport:
    def __init__(self, replies: dict[str, Any], *, raises: Exception | None = None):
        self.replies = replies
        self.raises = raises
        self.seen: list[tuple[str, KoraRequest, Mapping[str, str]]] = []

    def __call__(self, base_url: str, request: KoraRequest, headers: Mapping[str, str]):
        self.seen.append((base_url, request, headers))
        if self.raises is not None:
            raise self.raises
        return self.replies[request.method]


def _open(**replies: Any) -> tuple[KoraRelay, _Transport]:
    transport = _Transport(
        {"getPayerSigner": {"result": {"signer_address": RELAY}}, **replies}
    )
    return KoraRelay.open(
        "http://127.0.0.1:8080", api_key=SECRET, transport=transport
    ), transport


def test_open_pins_the_payer_the_node_names_and_satisfies_the_protocol() -> None:
    relay, transport = _open()
    assert relay.pubkey == RELAY
    assert isinstance(relay, FeePayerRelay)
    _url, request, headers = transport.seen[0]
    assert request.method == "getPayerSigner"
    assert request.params == {}, "a no-argument method takes an empty OBJECT"
    assert headers["x-api-key"] == SECRET
    assert SECRET not in repr(relay)


def test_sign_sends_named_params_and_returns_the_answer_untouched() -> None:
    relay, transport = _open(
        signTransaction={
            "result": {"signed_transaction": "QUJD", "signer_pubkey": RELAY}
        }
    )
    assert relay.sign_as_fee_payer("dW5zaWduZWQ=") == "QUJD"
    _url, request, _headers = transport.seen[-1]
    assert request.method == "signTransaction"
    assert isinstance(request.params, Mapping), "Kora refuses a positional list"
    assert request.params == {"transaction": "dW5zaWduZWQ=", "signer_key": RELAY}


def test_an_answer_from_another_signer_is_refused() -> None:
    relay, _ = _open(
        signTransaction={
            "result": {"signed_transaction": "QUJD", "signer_pubkey": "somebody-else"}
        }
    )
    with pytest.raises(KoraRelayError, match="other than the pinned payer"):
        relay.sign_as_fee_payer("dW5zaWduZWQ=")


def test_a_reply_without_bytes_is_refused() -> None:
    relay, _ = _open(signTransaction={"result": {"signer_pubkey": RELAY}})
    with pytest.raises(KoraRelayError, match="no signed_transaction"):
        relay.sign_as_fee_payer("dW5zaWduZWQ=")


def test_a_json_rpc_error_carries_only_its_code() -> None:
    relay, _ = _open(
        signTransaction={"error": {"code": -32000, "message": f"key {SECRET} bad"}}
    )
    with pytest.raises(KoraRelayError) as err:
        relay.sign_as_fee_payer("dW5zaWduZWQ=")
    assert "code=-32000" in str(err.value)
    assert SECRET not in str(err.value)


def test_a_transport_fault_is_a_refusal_naming_only_the_type() -> None:
    transport = _Transport({}, raises=ConnectionError(f"x-api-key {SECRET}"))
    with pytest.raises(KoraRelayError) as err:
        KoraRelay.open("http://127.0.0.1:8080", api_key=SECRET, transport=transport)
    assert "ConnectionError" in str(err.value)
    assert SECRET not in str(err.value)


def test_no_key_and_a_bad_scheme_refuse_before_any_call() -> None:
    transport = _Transport({})
    with pytest.raises(KoraRelayError, match="no API key"):
        KoraRelay.open("http://127.0.0.1:8080", api_key="", transport=transport)
    with pytest.raises(Exception):
        KoraRelay.open("file:///etc/passwd", api_key=SECRET, transport=transport)
    assert transport.seen == []


def test_sign_and_send_is_not_a_method_this_client_can_name() -> None:
    """The method is a constant; there is no parameter through which the broadcasting
    variant could be named. The docstring mentions it only to say it is absent."""
    import scripts.kora_relay as module

    assert module.SIGN_METHOD == "signTransaction"
    assert module.PAYER_METHOD == "getPayerSigner"
    source = Path(module.__file__).read_text()
    code_only = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"'))
    )
    # In code (not prose), the broadcasting method never appears as a string literal.
    assert '"signAndSendTransaction"' not in code_only
