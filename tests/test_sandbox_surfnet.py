"""The guard that stands between this repo and a mainnet signature.

Three things are proved here, and only the first needs a validator:

1. a REAL surfpool fork answers ``surfnet_getSurfnetInfo`` in the shape the module
   documents (marked ``fork``; deselect with ``-m "not fork"``);
2. an endpoint that does NOT implement the cheatcode is refused, and — the point —
   the refusal lands BEFORE any key material exists;
3. nothing that renders or raises out of an :class:`EphemeralSigner` carries the secret.

The offline legs use a light fake transport. No mainnet call is made by any test in
this file, and no test here signs anything that could be broadcast.
"""

from __future__ import annotations

import dataclasses

import copy
import os
import pickle
import re
from typing import Any

import pytest

from gecko.pda_testkit import (
    start_failure_is_a_broken_gate,
    SurfpoolError,
    SurfpoolFork,
    surfpool_status,
)
from gecko.rpc import RpcError
from gecko.sandbox import (
    SURFNET_INFO_METHOD,
    EphemeralSigner,
    EphemeralSignerError,
    NotASurfnetError,
    SurfnetProof,
    ephemeral_signer,
    prove_surfnet,
)
from gecko.sandbox import surfnet as surfnet_module

FORK_PORT = 8922
FORK_URL = "http://127.0.0.1:8899"

# The measured answer, copied verbatim from a live surfpool 1.1.1 fork. Every offline
# leg replays THIS rather than a shape we invented — if surfpool changes, the `fork`
# test below is what goes red, and the fixture is what gets corrected.
MEASURED_INFO_RESULT: dict[str, Any] = {
    "context": {"slot": 439792934, "apiVersion": "3.1.6"},
    "value": {"runbookExecutions": []},
}


def surfnet_transport(calls: list[str] | None = None) -> Any:
    """A fake RPC that answers the cheatcode exactly as the real fork did."""

    def call(_url: str, method: str, params: list[Any]) -> dict[str, Any]:
        if calls is not None:
            calls.append(f"rpc:{method}")
        assert params == [], "measured: surfnet_getSurfnetInfo takes NO parameters"
        if method != SURFNET_INFO_METHOD:
            raise RpcError(
                f"JSON-RPC {method} failed: code=-32601 message='Method not found'"
            )
        return {"jsonrpc": "2.0", "id": 1, "result": MEASURED_INFO_RESULT}

    return call


def method_not_found_transport(calls: list[str] | None = None) -> Any:
    """A stock Agave/mainnet RPC: it has never heard of the cheatcode."""

    def call(_url: str, method: str, _params: list[Any]) -> dict[str, Any]:
        if calls is not None:
            calls.append(f"rpc:{method}")
        raise RpcError(
            f"JSON-RPC {method} failed: code=-32601 message='Method not found'"
        )

    return call


# --- the structural guard: no key can exist before the proof -----------------


def test_refusal_lands_before_any_key_material_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An RPC that does not implement the cheatcode gets a refusal and NO keypair.

    This is the whole safety property. ``_generate_keypair`` is the one place a secret
    comes into existence; if it is ever reached on this path, a key has been created for
    an endpoint that never proved itself — which on a misconfigured URL is mainnet.
    """
    keygen_calls: list[str] = []
    monkeypatch.setattr(
        surfnet_module,
        "_generate_keypair",
        lambda: keygen_calls.append("keygen"),  # type: ignore[arg-type,return-value]
    )

    with pytest.raises(NotASurfnetError) as excinfo:
        prove_surfnet(
            "https://api.mainnet-beta.solana.com", method_not_found_transport()
        )

    assert keygen_calls == [], "a key was generated for an unproven endpoint"
    assert "api.mainnet-beta.solana.com" in str(excinfo.value), (
        "refusal must name the URL"
    )


def test_the_proof_happens_before_the_keygen_and_the_order_is_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutation this catches: generate the key first, prove afterwards.

    Both events append to one list, so the assertion is on ORDER, not on presence. Move
    the ``Keypair()`` call ahead of the RPC — into ``prove_surfnet``, or into an
    ``ephemeral_signer`` that took a URL — and the recorded sequence inverts.
    """
    events: list[str] = []
    real_generate = surfnet_module._generate_keypair

    def recording_generate() -> Any:
        events.append("keygen")
        return real_generate()

    monkeypatch.setattr(surfnet_module, "_generate_keypair", recording_generate)

    signer = ephemeral_signer(prove_surfnet(FORK_URL, surfnet_transport(events)))

    assert events == [f"rpc:{SURFNET_INFO_METHOD}", "keygen"]
    assert signer.rpc_url == FORK_URL


def test_ephemeral_signer_cannot_be_called_with_a_url() -> None:
    """ "Sign without proving" has no spelling: the argument is a proof, not an endpoint.

    A raw string is not a :class:`SurfnetProof`, so the unsafe program is refused at the
    constructor rather than reaching key generation.
    """
    with pytest.raises(EphemeralSignerError) as excinfo:
        ephemeral_signer("https://api.mainnet-beta.solana.com")  # type: ignore[arg-type]
    assert "no key was created" in str(excinfo.value)


def test_a_hand_built_proof_must_still_carry_real_surfnet_evidence() -> None:
    """The type is not a rubber stamp — the constructor re-runs the evidence check.

    Without this, the guard is one ``SurfnetProof(mainnet_url, {})`` away from useless.
    """
    with pytest.raises(NotASurfnetError):
        SurfnetProof(rpc_url="https://api.mainnet-beta.solana.com", detail={})
    with pytest.raises(NotASurfnetError):
        SurfnetProof(rpc_url=FORK_URL, detail={"context": {"slot": 1}, "value": {}})
    with pytest.raises(NotASurfnetError):
        # a canned blob with no live clock is not a running validator
        SurfnetProof(
            rpc_url=FORK_URL,
            detail={"context": {}, "value": {"runbookExecutions": []}},
        )


def test_a_non_http_endpoint_is_refused_before_the_transport_runs() -> None:
    calls: list[str] = []
    with pytest.raises(RpcError):
        prove_surfnet("file:///etc/passwd", surfnet_transport(calls))
    assert calls == []


def test_any_transport_failure_fails_closed_and_names_the_url() -> None:
    def exploding(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
        raise TimeoutError("connection timed out")

    with pytest.raises(NotASurfnetError) as excinfo:
        prove_surfnet("http://127.0.0.1:1/", exploding)
    assert "127.0.0.1:1" in str(excinfo.value)
    assert "TimeoutError" in str(excinfo.value)


def test_a_wrong_shaped_answer_is_refused() -> None:
    def liar(_url: str, _method: str, _params: list[Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

    with pytest.raises(NotASurfnetError):
        prove_surfnet(FORK_URL, liar)


def test_the_proof_is_frozen_so_it_cannot_be_edited_after_the_check() -> None:
    proof = prove_surfnet(FORK_URL, surfnet_transport())
    assert proof.slot == MEASURED_INFO_RESULT["context"]["slot"]
    with pytest.raises(TypeError):
        proof.detail["value"] = {}  # type: ignore[index]


# --- no key material may escape ----------------------------------------------


def signer_for_tests() -> EphemeralSigner:
    return ephemeral_signer(prove_surfnet(FORK_URL, surfnet_transport()))


def secret_encodings(signer: EphemeralSigner) -> list[str]:
    """Every textual form the secret could plausibly take, for a leak search.

    Reaching into the private slot is exactly what production code must never do; a test
    is the one place it is legitimate, because the assertion is that these strings appear
    NOWHERE in anything the object emits.
    """
    keypair = signer._keypair
    raw = bytes(keypair.to_bytes())
    seed = bytes(keypair.secret())
    forms = [raw.hex(), seed.hex(), str(list(raw)), str(list(seed))]
    forms.append(str(keypair.to_json()))
    return forms


def test_repr_and_str_do_not_leak_the_secret() -> None:
    signer = signer_for_tests()
    rendered = f"{signer!r} {signer} {format(signer)}"
    for form in secret_encodings(signer):
        assert form not in rendered
    assert signer.pubkey in rendered
    assert "redacted" in rendered


def test_the_signer_is_never_serialised() -> None:
    signer = signer_for_tests()
    with pytest.raises(EphemeralSignerError):
        pickle.dumps(signer)
    with pytest.raises(EphemeralSignerError):
        copy.copy(signer)
    with pytest.raises(EphemeralSignerError):
        copy.deepcopy(signer)
    with pytest.raises(TypeError):
        vars(signer)  # __slots__: there is no __dict__ to hand the keypair back through


def test_a_raised_exception_carries_no_key_bytes() -> None:
    signer = signer_for_tests()
    forms = secret_encodings(signer)

    messages: list[str] = []
    for bad in (None, "not bytes", 42, b""):
        with pytest.raises(EphemeralSignerError) as excinfo:
            signer.sign(bad)  # type: ignore[arg-type]
        messages.append(str(excinfo.value))

    joined = " ".join(messages)
    for form in forms:
        assert form not in joined


def test_an_upstream_error_message_is_redacted_by_pattern() -> None:
    """The realistic leak: a library echoes the key into its own error string.

    ``sign`` must not pass that through. The fake keypair below raises with a secret
    embedded in three encodings; none may survive into the message the caller sees.
    """
    signer = signer_for_tests()
    secret_hex = "ab" * 32
    secret_b58 = "5" * 60

    class LeakyKeypair:
        def sign_message(self, _message: bytes) -> bytes:
            raise ValueError(
                f"bad key hex={secret_hex} b58={secret_b58} bytes={[7] * 32}"
            )

    object.__setattr__(signer, "_keypair", LeakyKeypair())
    with pytest.raises(EphemeralSignerError) as excinfo:
        signer.sign(b"message")

    message = str(excinfo.value)
    assert secret_hex not in message
    assert secret_b58 not in message
    assert "[7, 7," not in message
    assert "<redacted>" in message
    assert "ValueError" in message


def test_signing_still_works_and_verifies_against_the_public_key() -> None:
    """The signer must actually sign — a guard that broke the primitive is not a guard."""
    solders_pubkey = pytest.importorskip("solders.pubkey")
    solders_signature = pytest.importorskip("solders.signature")

    signer = signer_for_tests()
    message = b"gecko sandbox rehearsal"
    signature = signer.sign(message)

    assert isinstance(signature, bytes) and len(signature) == 64
    parsed = solders_signature.Signature.from_bytes(signature)
    assert parsed.verify(solders_pubkey.Pubkey.from_string(signer.pubkey), message)
    # different call, different key: the signer is ephemeral, not a stored identity
    assert signer_for_tests().pubkey != signer.pubkey


def test_redaction_leaves_ordinary_diagnostics_readable() -> None:
    text = "JSON-RPC surfnet_getSurfnetInfo failed: code=-32601 at slot 439792934"
    assert surfnet_module._redact(text) == text


# --- the real fork: the shape in the module docstring is measured, not guessed -


@pytest.mark.fork
@pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=(
        "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — THE FORK LEG DID NOT RUN and "
        "the getSurfnetInfo shape below was NOT re-measured: "
        + surfpool_status().detail
    ),
)
def test_a_real_surfpool_fork_proves_itself_in_the_documented_shape() -> None:
    """Fork a mainnet RPC locally and re-measure the cheatcode's answer.

    This is the test that keeps the module docstring honest: it asserts the exact
    response structure recorded there, and it is what goes red if a surfpool upgrade
    changes it. Nothing is signed and nothing is broadcast — the fork is a local
    validator and the only call made is a read.
    """
    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=120) as fork:
            proof = prove_surfnet(fork.rpc_url)
            signer = ephemeral_signer(proof)
            signature = signer.sign(b"proved surfnet, local key, nothing broadcast")
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")

    assert proof.rpc_url == f"http://127.0.0.1:{FORK_PORT}"
    assert set(proof.detail) == {"context", "value"}
    assert set(proof.detail["context"]) >= {"slot", "apiVersion"}
    assert isinstance(proof.detail["context"]["slot"], int)
    assert proof.detail["context"]["slot"] > 0
    assert tuple(proof.detail["value"]["runbookExecutions"]) == ()
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(proof.detail["context"]["apiVersion"]))
    assert len(signature) == 64


@pytest.mark.fork
@pytest.mark.skipif(
    not surfpool_status().available or os.getenv("GECKO_FORK_DEMO") == "0",
    reason=(
        "surfpool is not on PATH (or GECKO_FORK_DEMO=0) — the params/method-not-found "
        "measurements below were NOT re-run: " + surfpool_status().detail
    ),
)
def test_the_fork_rejects_params_and_answers_32601_for_an_unknown_method() -> None:
    """The two measurements the offline fakes are built on, re-checked against reality.

    If ``surfnet_getSurfnetInfo`` ever starts accepting a config object, or an unknown
    method stops answering ``-32601``, the fakes in this file are lying and this is
    where that shows up.
    """
    from gecko.rpc import default_rpc_call

    mainnet = os.getenv("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")
    try:
        with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=120) as fork:
            with pytest.raises(RpcError) as params_exc:
                default_rpc_call(fork.rpc_url, SURFNET_INFO_METHOD, [{}])
            with pytest.raises(RpcError) as unknown_exc:
                default_rpc_call(fork.rpc_url, "surfnet_thisDoesNotExist", [])
    except SurfpoolError as exc:
        # Installed and would not start = a BROKEN gate, not an absent one.
        if start_failure_is_a_broken_gate():
            pytest.fail(
                "surfpool IS installed and the fork did not start, so this "
                "gate is broken rather than absent — a skip here would claim "
                f"the environment cannot do what it demonstrably can: {exc}"
            )
        pytest.skip(f"THE FORK LEG DID NOT RUN — surfpool never became ready: {exc}")

    assert "-32602" in str(params_exc.value)
    assert "No parameters were expected" in str(params_exc.value)
    assert "-32601" in str(unknown_exc.value)


# ---------------------------------------------------------------------------
# the four paths an adversarial pass used to reach a MAINNET-BOUND key, plus the
# fifth that the first fix left open. Each one signed, or would have.
# ---------------------------------------------------------------------------

MAINNET = "https://api.mainnet-beta.solana.com"


def _minted(rpc_url: str = "http://127.0.0.1:8899") -> SurfnetProof:
    """A genuine proof, minted through the only path that mints one."""
    return prove_surfnet(
        rpc_url, rpc_call=lambda url, method, params: {"result": MEASURED_INFO_RESULT}
    )


def test_a_hand_built_proof_for_mainnet_creates_no_key() -> None:
    """The first defence was `__post_init__` re-validating the evidence. Not enough.

    Re-validation proves the PAYLOAD is a surfnet's. It says nothing about whether the
    URL beside it is the endpoint that produced it, and that gap was the whole hole.
    """
    with pytest.raises(EphemeralSignerError):
        ephemeral_signer(SurfnetProof(rpc_url=MAINNET, detail=MEASURED_INFO_RESULT))


def test_replace_cannot_repoint_a_genuine_proof_at_mainnet() -> None:
    """The one-liner that defeated the original design, and it forged nothing.

    `dataclasses.replace(real_proof, rpc_url=MAINNET)` carries the evidence a real fork
    actually returned and swaps one string. The old `__post_init__` check passed it, all
    three downstream bindings passed it — because every binding compares against
    `proof.rpc_url` itself — and the real rehearsal issued
    `sendTransaction -> https://api.mainnet-beta.solana.com`.
    """
    with pytest.raises((NotASurfnetError, EphemeralSignerError)):
        ephemeral_signer(dataclasses.replace(_minted(), rpc_url=MAINNET))


def test_setattr_cannot_repoint_a_genuine_proof() -> None:
    """`frozen=True` stops assignment, not `object.__setattr__`."""
    proof = _minted()
    object.__setattr__(proof, "rpc_url", MAINNET)
    with pytest.raises(EphemeralSignerError):
        ephemeral_signer(proof)


def test_a_subclass_cannot_skip_the_evidence_check() -> None:
    """Overriding `__post_init__` to `pass` removed the only check there was."""

    class Sneaky(SurfnetProof):
        def __post_init__(self) -> None:  # noqa: D105 — the attack IS the absence
            pass

    with pytest.raises(EphemeralSignerError):
        ephemeral_signer(Sneaky(MAINNET, {}))


def test_an_attestation_built_outside_prove_surfnet_is_not_accepted() -> None:
    """The gap the FIRST fix left, found by re-running the attacks against it.

    `_Attestation`'s leading underscore is a convention, not a wall — any in-repo caller
    can import and construct one. Identity against `proof.rpc_url` alone therefore still
    accepted a self-issued ticket. Membership in the weak registry of attestations this
    module actually minted cannot be arranged that way: getting in requires having made
    the round trip.
    """
    forged = SurfnetProof(
        rpc_url="http://127.0.0.1:8899",
        detail=MEASURED_INFO_RESULT,
        # reached through the module on purpose: the underscore is a convention,
        # and this test exists because a convention is not a wall.
        attestation=surfnet_module._Attestation("http://127.0.0.1:8899"),
    )
    with pytest.raises(EphemeralSignerError):
        ephemeral_signer(forged)


def test_the_genuine_path_still_produces_a_signer() -> None:
    """A guard that refuses everything is not a guard, it is an outage."""
    signer = ephemeral_signer(_minted())
    assert signer.rpc_url == "http://127.0.0.1:8899"
    assert len(signer.pubkey) >= 32
