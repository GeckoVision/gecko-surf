"""What the two purchase scripts actually prove — and the check that could not fail.

These are the scripts that moved real money (mainnet signature
``5XHQCMgbN3uBQpgA7HJJNsC6S1r2b4QHNcvturPn9EoEV1FMSJCExcmrMff2m36vyDeTrw5XR57VqBQTp4RNyCrM``,
slot 438494959, 36,399 CU predicted and consumed), and they are the path an autonomous
agent would walk. They are also the two files ``mypy gecko`` never reads, so what they
prove has to be asserted here or nowhere.

THE HONEST ASSESSMENT, which these tests hold in place:

* ``scripts/prepare_purchase.py`` simulates the builder's ``serializedTransaction`` and
  then calls ``evaluate_tx`` on that SAME string. Presented and attested are computed from
  one variable, so the digest halves are equal by construction — that half of the call
  cannot fail. What the call does decide is real and worth keeping: the receipt passed,
  it carries an ``exact`` binding rather than a structural one, it names the network the
  operator named, and the message loads no accounts from a lookup table. The identity
  half is a self-check, and this file makes the script SAY so.
* ``scripts/sign_and_send.py`` re-simulates at signing time. That re-simulation is not
  ceremony — it catches a transaction that has gone stale, reverts against current state,
  or was never valid — but its fresh receipt is derived from the very bytes it then
  checks, so it too cannot refuse on identity. A swapped recipient simulates just fine.

The gap those two facts leave open is the whole subject of this file: nothing carried a
claim ACROSS the gap between the two terminals, so bytes could be replaced in transit and
both scripts would approve the replacement. The falsifiable comparison is the ORIGINAL
receipt's binding, recorded before the bytes travelled, checked against the fresh one.

Offline (Pattern B): every transaction is assembled locally, every RPC is injected, and
no test here signs, sends, or reads a real key.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import pytest

from gecko.landing import assemble_unsigned_tx
from gecko.txbind import _B58
from test_store_accounts import node_with

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Any URL that survives the SSRF check — the transport underneath is injected, so the
#: hostname is decoration. That is the point: a URL is evidence of nothing.
RPC_URL = "https://rpc.example.com"
#: A mainnet-SHAPED hostname in front of an injected (fork-shaped) transport.
MAINNET_SHAPED_URL = "https://api.mainnet-beta.solana.com"

#: A real base58 blockhash, generated once and pinned so a run is reproducible. Nothing
#: here goes near a network to get one.
BLOCKHASH = "4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofM"

#: Where the money was supposed to go, and where an attacker would rather it went. Two
#: transactions identical in every byte except one account key — the exact shape the
#: binding exists to catch.
HONEST_RECIPIENT = "FaK5981JTnAbraeKQTjptKAHiF74Zy4upg2hoBdLnGyY"
SWAPPED_RECIPIENT = "DLkcqeNNX8nRQgD87DN7LjHkcLQd9K2wuqaCbhkERJxL"

_BINDING_LINE = re.compile(r"binding\s+([0-9a-f]{64})")


def _b58(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    digits = ""
    while number:
        number, remainder = divmod(number, 58)
        digits = _B58[remainder] + digits
    return "1" * (len(raw) - len(raw.lstrip(b"\x00"))) + digits


def _transfer_tx(payer: str, recipient: str) -> str:
    """A real unsigned legacy transaction, base58 — one lamport transfer."""
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer

    instruction = transfer(
        TransferParams(
            from_pubkey=Pubkey.from_string(payer),
            to_pubkey=Pubkey.from_string(recipient),
            lamports=100_000,
        )
    )
    built = assemble_unsigned_tx([instruction], payer, blockhash=BLOCKHASH)
    return _b58(base64.b64decode(built.tx))


def _keypair_file(tmp_path: Path) -> tuple[Path, str]:
    """A throwaway keypair on disk, exactly as ``solana-keygen`` writes one.

    It never signs in this file: every run stops at ``--send`` being absent. It exists
    because ``sign_and_send`` refuses a transaction whose fee payer it cannot pay for,
    and that refusal must not be what a test is accidentally measuring.
    """
    from solders.keypair import Keypair

    keypair = Keypair()
    path = tmp_path / "id.json"
    path.write_text(json.dumps(list(bytes(keypair))))
    return path, str(keypair.pubkey())


def _store_rpc(*, err: Any = None) -> Callable[[str, str, Any], dict[str, Any]]:
    """``_sim_rpc`` plus the one store account ``prepare_purchase`` now reads.

    The script resolves ``--store`` from the chain instead of pasting three constants, so
    its offline transport has to serve that account. Still injected, still no network: the
    bytes come from the same IDL-shaped encoder the store-directory tests use.
    """
    simulate_call = _sim_rpc(err=err)
    store = node_with("jonasbar")

    def call(url: str, method: str, params: Any) -> dict[str, Any]:
        if method == "getAccountInfo":
            return store(url, method, params)
        return simulate_call(url, method, params)

    return call


def _sim_rpc(*, err: Any = None) -> Callable[[str, str, Any], dict[str, Any]]:
    """A transport that answers a passing simulation and nothing else."""

    def call(_url: str, method: str, _params: Any) -> dict[str, Any]:
        if method == "simulateTransaction":
            return {
                "result": {
                    "context": {"slot": 438_494_959},
                    "value": {
                        "err": err,
                        "logs": ["Program 11111111111111111111111111111111 success"],
                        "unitsConsumed": 36_399,
                    },
                }
            }
        if method == "getSlot":
            return {"result": 438_494_959}
        return {"result": {"value": None}}

    return call


def _run(
    main: Callable[[list[str]], int],
    argv: Sequence[str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    """Run a script's ``main`` and report (exit code, everything it printed).

    ``SystemExit`` is folded into the code because argparse refusing to start is a
    refusal like any other — what matters to every assertion below is that a run either
    approves or does not, never how it declined.
    """
    try:
        code = main(list(argv))
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    signer: str,
    tx: str,
    network: str = "mainnet",
    rpc_url: str = RPC_URL,
) -> tuple[int, str]:
    """Run ``prepare_purchase`` with the builder, the blockhash read and the transport
    injected — the real ``simulate`` and the real gate still run."""
    import scripts.prepare_purchase as prepare

    monkeypatch.setattr(prepare, "token_balance", lambda *_a, **_k: 0.5)
    monkeypatch.setattr(
        prepare,
        "build_instruction",
        lambda *_a, **_k: {
            "serializedTransaction": tx,
            "encoding": "base58",
            "simulationError": None,
            "riskLevel": "low",
            "wireFormat": "legacy",
        },
    )
    monkeypatch.setattr(
        prepare, "latest_blockhash", lambda *_a, **_k: (BLOCKHASH, 1_000)
    )
    monkeypatch.setattr(prepare, "priority_fee_microlamports", lambda *_a, **_k: 5_000)
    monkeypatch.setattr(prepare, "_rpc", _store_rpc())

    return _run(
        prepare.main,
        ["--signer", signer, "--rpc-url", rpc_url, "--network", network],
        capsys,
    )


def _sign(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: Iterable[str],
    *,
    seen: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Run ``sign_and_send`` against an injected transport. Never with ``--send``."""
    import scripts.sign_and_send as sign

    monkeypatch.setattr(sign, "_rpc", _sim_rpc())
    if seen is not None:
        real_simulate = sign.simulate

        def recording_simulate(plan: Any, **kwargs: Any) -> Any:
            seen["network_arg"] = kwargs.get("network")
            receipt = real_simulate(plan, **kwargs)
            seen["receipt"] = receipt
            return receipt

        monkeypatch.setattr(sign, "simulate", recording_simulate)

    argv = list(argv)
    assert "--send" not in argv, "no test in this file may broadcast"
    return _run(sign.main, argv, capsys)


def _binding_of(output: str) -> str:
    match = _BINDING_LINE.search(output)
    assert match, f"the prepare run printed no binding to carry:\n{output}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# What the prepare-side check proves — stated, and asserted.
# --------------------------------------------------------------------------- #
def test_the_prepare_side_identity_check_compares_a_value_with_itself(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The two halves of the prepare-side comparison come from one string.

    ``serialized`` is fed to ``simulate`` (through ``build_call``) and then handed to
    ``evaluate_tx``; the digest is a pure function of those bytes, so presented ==
    attested with no way for it to be otherwise. This is not a complaint about
    ``evaluate_tx`` — the status, strength, network and lookup gates in the same call are
    real. It is the reason the script must not describe the identity half as verification,
    and must instead emit a binding that a LATER, independent step can fail on.
    """
    _keypair, signer = _keypair_file(tmp_path)
    code, output = _prepare(
        monkeypatch, capsys, signer=signer, tx=_transfer_tx(signer, HONEST_RECIPIENT)
    )

    assert code == 0
    assert "self-check" in output.lower(), (
        "the prepare run still presents a comparison of a value with itself as a "
        "verification:\n" + output
    )
    assert "APPROVED TO SIGN" in output


def test_prepare_emits_the_binding_and_the_command_that_carries_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The binding has to LEAVE this terminal, or nothing downstream can check anything.

    The printed next command is the artifact: it names ``--expect-binding`` and the exact
    digest, so the signing step is handed a claim it did not derive from the bytes in
    front of it.
    """
    _keypair, signer = _keypair_file(tmp_path)
    _code, output = _prepare(
        monkeypatch, capsys, signer=signer, tx=_transfer_tx(signer, HONEST_RECIPIENT)
    )
    binding = _binding_of(output)

    assert "--expect-binding" in output
    assert f"--expect-binding {binding}" in output, (
        "the printed command does not carry the binding this run computed:\n" + output
    )
    # Never echo a keyed RPC URL back into a terminal transcript or a demo recording.
    assert RPC_URL not in output.split("uv run python scripts/sign_and_send.py", 1)[-1]


# --------------------------------------------------------------------------- #
# THE BUG. A transaction swapped between the two terminals, and a signing script that
# has nothing to compare it against.
# --------------------------------------------------------------------------- #
def test_the_signing_script_never_approves_bytes_it_was_not_told_to_expect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """THE REPRODUCTION. Prepare approved one transaction; the signer is handed another.

    The swapped transaction is well-formed, pays from the same fee payer, and simulates
    clean — a swapped recipient always does. The re-simulation at signing time computes a
    fresh receipt FROM THOSE BYTES and then asks whether it attests them, which it does,
    because it was built from them. With no claim carried across the gap the run has
    nothing that can disagree, and it approves the attacker's transaction.
    """
    keypair, signer = _keypair_file(tmp_path)
    swapped = _transfer_tx(signer, SWAPPED_RECIPIENT)

    code, output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            swapped,
            "--rpc-url",
            RPC_URL,
            "--network",
            "mainnet",
            "--keypair",
            str(keypair),
        ],
    )

    assert code != 0, (
        "the signing script approved a transaction nobody prepared — the only thing it "
        "checked was that a receipt it derived from these bytes matches these bytes:\n"
        + output
    )
    assert "Verified and NOT sent" not in output


def test_the_carried_binding_refuses_the_swapped_recipient(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The end-to-end seam: prepare one transaction, sign a different one, get refused.

    Both sides are real runs of the two scripts. The only thing crossing between them is
    the binding string, and it is the only value in the signing run that was not derived
    from ``--tx``.
    """
    keypair, signer = _keypair_file(tmp_path)
    honest = _transfer_tx(signer, HONEST_RECIPIENT)
    swapped = _transfer_tx(signer, SWAPPED_RECIPIENT)
    assert honest != swapped

    _code, prepared = _prepare(monkeypatch, capsys, signer=signer, tx=honest)
    binding = _binding_of(prepared)

    code, output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            swapped,
            "--expect-binding",
            binding,
            "--rpc-url",
            RPC_URL,
            "--network",
            "mainnet",
            "--keypair",
            str(keypair),
        ],
    )

    assert code == 1
    assert "DO NOT SIGN" in output
    assert binding in output, "the refusal does not show the claim that was broken"


def test_the_bytes_handed_to_the_signature_are_the_bytes_that_were_checked() -> None:
    """After approval the script re-derives its message from ``verify_handoff``'s payload
    rather than from the string it parsed at the top of the run.

    That payload is the SUBJECT re-encoded, so the two are byte-identical today — the
    point is that they stay identical BY CONSTRUCTION rather than by coincidence, because
    the gate hands back what it checked. Asserted at the ``gecko`` level: no test in this
    file signs, and the ``--send`` branch is never taken here.
    """
    from gecko.handoff import verify_handoff
    from gecko.simulate import BuiltTx, simulate
    from gecko.txbind import _b58decode

    honest = _transfer_tx(HONEST_RECIPIENT, SWAPPED_RECIPIENT)
    receipt = simulate(
        {},
        rpc_url=RPC_URL,
        rpc_call=_sim_rpc(),
        build_call=lambda _plan: BuiltTx(tx=honest, encoding="base58"),
        replace_blockhash=False,
        network="mainnet",
    )
    handoff = verify_handoff(
        honest,
        receipt,
        encoding="base58",
        require="exact",
        expected_network="mainnet",
    )

    assert handoff.approved
    assert handoff.transaction_base64 is not None
    assert base64.b64decode(handoff.transaction_base64) == _b58decode(honest)


def test_the_carried_binding_approves_the_bytes_it_was_taken_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The comparison must be able to PASS, or refusing everything would score as safe.

    Same transaction, same blockhash, unchanged in transit: the fresh binding equals the
    carried one and the run reaches its stopping point — verified, and not sent, because
    nothing in this file passes ``--send``.
    """
    keypair, signer = _keypair_file(tmp_path)
    honest = _transfer_tx(signer, HONEST_RECIPIENT)

    _code, prepared = _prepare(monkeypatch, capsys, signer=signer, tx=honest)
    binding = _binding_of(prepared)

    code, output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            honest,
            "--expect-binding",
            binding,
            "--rpc-url",
            RPC_URL,
            "--network",
            "mainnet",
            "--keypair",
            str(keypair),
        ],
    )

    assert code == 0
    assert "Verified and NOT sent" in output
    assert "SENT" not in output


def test_a_mistyped_binding_refuses_rather_than_being_normalised_away(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Surrounding whitespace and case are copy-paste noise; a changed DIGIT is not.

    A gate that "helpfully" repaired a wrong digest would be back to approving on
    nothing, so only the noise is tolerated.
    """
    keypair, signer = _keypair_file(tmp_path)
    honest = _transfer_tx(signer, HONEST_RECIPIENT)
    _code, prepared = _prepare(monkeypatch, capsys, signer=signer, tx=honest)
    binding = _binding_of(prepared)

    noisy = f"  {binding.upper()}\n"
    code, _output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            honest,
            "--expect-binding",
            noisy,
            "--rpc-url",
            RPC_URL,
            "--network",
            "mainnet",
            "--keypair",
            str(keypair),
        ],
    )
    assert code == 0

    wrong = ("0" if binding[0] != "0" else "1") + binding[1:]
    code, output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            honest,
            "--expect-binding",
            wrong,
            "--rpc-url",
            RPC_URL,
            "--network",
            "mainnet",
            "--keypair",
            str(keypair),
        ],
    )
    assert code == 1
    assert "DO NOT SIGN" in output


def test_the_expected_binding_is_required_and_carries_no_default() -> None:
    """A default here would be the whole fix wearing a flag: the value that makes the
    comparison possible cannot be one the script supplies to itself.

    A source check, so it is a lint against reintroduction rather than a proof — any
    string construction defeats it, and the behavioural tests above are what actually
    hold the gate.
    """
    source = (REPO_ROOT / "scripts" / "sign_and_send.py").read_text()
    declaration = re.search(
        r"add_argument\(\s*[\"']--expect-binding[\"'](.*?)\n\s*\)", source, re.S
    )
    assert declaration, "sign_and_send.py does not declare --expect-binding"
    body = declaration.group(1)
    assert "required=True" in body
    assert "default=" not in body


# --------------------------------------------------------------------------- #
# The network, on the script path. A URL is evidence of nothing, here too.
# --------------------------------------------------------------------------- #
def test_the_signing_script_refuses_when_the_operator_named_no_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``unknown`` is a legal answer and a refusal — a correct binding does not rescue it.

    The carried binding matches, the simulation passes, and the run still hands over
    nothing, because a receipt from a network nobody named attests nothing about the one
    a signature is headed for.
    """
    keypair, signer = _keypair_file(tmp_path)
    honest = _transfer_tx(signer, HONEST_RECIPIENT)
    _code, prepared = _prepare(monkeypatch, capsys, signer=signer, tx=honest)
    binding = _binding_of(prepared)

    code, output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            honest,
            "--expect-binding",
            binding,
            "--rpc-url",
            RPC_URL,
            "--network",
            "unknown",
            "--keypair",
            str(keypair),
        ],
    )

    assert code == 1
    assert "DO NOT SIGN" in output


def test_a_fork_run_behind_a_mainnet_shaped_url_records_fork_and_clears_no_mainnet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The transport is a fork and the hostname says mainnet. The operator said ``fork``,
    so the receipt says ``fork`` — the URL contributed nothing.

    The residual, stated rather than hidden: an operator who says ``mainnet`` while
    pointed at a fork is BELIEVED by these scripts. Closing that needs a chain-identity
    probe (a genesis-hash read, itself from an untrusted node), and inferring it from the
    URL is exactly the fabrication the network vocabulary forbids. What is closed here is
    that the fork run's receipt can never clear a mainnet signature.
    """
    from gecko.txbind import evaluate_tx

    keypair, signer = _keypair_file(tmp_path)
    honest = _transfer_tx(signer, HONEST_RECIPIENT)
    _code, prepared = _prepare(
        monkeypatch,
        capsys,
        signer=signer,
        tx=honest,
        network="fork",
        rpc_url=MAINNET_SHAPED_URL,
    )
    binding = _binding_of(prepared)

    seen: dict[str, Any] = {}
    code, _output = _sign(
        monkeypatch,
        capsys,
        [
            "--tx",
            honest,
            "--expect-binding",
            binding,
            "--rpc-url",
            MAINNET_SHAPED_URL,
            "--network",
            "fork",
            "--keypair",
            str(keypair),
        ],
        seen=seen,
    )

    assert code == 0
    assert seen["network_arg"] == "fork"
    receipt = seen["receipt"]
    assert receipt.network == "fork"
    assert (
        evaluate_tx(
            honest,
            receipt,
            encoding="base58",
            require="exact",
            expected_network="mainnet",
        ).approved
        is False
    )


def test_no_purchase_script_hardcodes_a_network_it_was_not_told() -> None:
    """The scripts are operators, so they READ a network; they may never state one.

    Narrower than the repo-wide check in ``tests/test_network_vocabulary.py`` and kept
    beside the two files this task edits: an approvable literal bound to a
    ``network``-shaped keyword here is a fabricated assertion, and a ``--network``
    default is the same fabrication with a friendlier face.
    """
    fabricated = re.compile(r"\bnetwork\s*=\s*[\"'](mainnet|devnet|testnet|fork)[\"']")

    for name in ("prepare_purchase.py", "sign_and_send.py"):
        source = (REPO_ROOT / "scripts" / name).read_text()
        offenders = [
            f"{name}:{number}: {line.strip()}"
            for number, line in enumerate(source.splitlines(), start=1)
            if fabricated.search(line)
        ]
        assert not offenders, "\n".join(offenders)

        declaration = re.search(
            r"add_argument\(\s*[\"']--network[\"'](.*?)\n\s*\)", source, re.S
        )
        assert declaration, f"{name} does not declare --network"
        assert "required=True" in declaration.group(1)
        assert "default=" not in declaration.group(1)
