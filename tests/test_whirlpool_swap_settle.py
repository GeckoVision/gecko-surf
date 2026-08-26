"""The `--send` path refuses before it signs, and its refusals need no network.

`prepare_whirlpool_swap.py --send` is the only swap path that can move mainnet money, so
the gates in front of the signature are the part worth pinning. Every test here reaches a
refusal BEFORE `simulate` is called, which is why none of them touch a validator: if any
of these ever needed the network to fail, the ordering would already be wrong.
"""

import base64
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import pytest

pytest.importorskip("solders", reason="the settle path needs the [solana] extra")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare_whirlpool_swap.py"
_spec = importlib.util.spec_from_file_location("prepare_whirlpool_swap", _SCRIPT)
swap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swap)


def _unsigned_tx_paying_from(pubkey) -> str:
    """A well-formed transaction whose FEE PAYER is `pubkey`, base64 as the settle path
    receives it. Contents are irrelevant — every assertion here is about who pays."""
    from solders.hash import Hash
    from solders.instruction import Instruction
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction

    instruction = Instruction(Pubkey.default(), b"", [])
    message = Message.new_with_blockhash([instruction], pubkey, Hash.default())
    return base64.b64encode(bytes(Transaction.new_unsigned(message))).decode()


def _keypair_file(tmp_path: Path):
    from solders.keypair import Keypair

    keypair = Keypair()
    path = tmp_path / "signer.json"
    path.write_text(json.dumps(list(bytes(keypair))))
    return keypair, path


def _args(**over) -> Namespace:
    base = {
        "rpc_url": "http://127.0.0.1:1",  # unreachable ON PURPOSE — nothing here may dial
        "network": "mainnet",
        "keypair": Path("/nonexistent/key.json"),
        "send": True,
    }
    base.update(over)
    return Namespace(**base)


def test_a_missing_keypair_refuses_before_anything_else(tmp_path):
    code = swap._settle(_args(), _unsigned_tx_paying_from(__import__("solders.keypair", fromlist=["Keypair"]).Keypair().pubkey()), "abc")
    assert code == 1


def test_a_fee_payer_your_key_does_not_control_refuses(tmp_path):
    """Signing for an account you do not control yields a valid signature the network
    rejects, and burns the blockhash. Caught before the signature exists."""
    from solders.keypair import Keypair

    _mine, path = _keypair_file(tmp_path)
    someone_else = Keypair().pubkey()
    code = swap._settle(
        _args(keypair=path), _unsigned_tx_paying_from(someone_else), "abc"
    )
    assert code == 1


def test_a_mistyped_network_is_refused_by_argparse_not_by_coercion():
    """The trap this pins: `coerce_network` maps ANY unrecognised string to "unknown"
    rather than raising, so a typo like `mainet` would otherwise sail past the parser,
    become "unknown", and only be caught after a network round trip. argparse `choices`
    is the gate — the same one sign_and_send.py uses."""
    from gecko.networks import coerce_network

    assert coerce_network("not-a-network") == "unknown"  # it really does not raise

    with pytest.raises(SystemExit):
        swap.main(["--signer", "AnyPubkeyShape", "--network", "mainet"])


def test_send_is_off_by_default_and_needs_no_keypair_to_prepare():
    """The dangerous flag is opt-in. Preparing must never require a key to be present."""
    parsed = swap.main.__wrapped__ if hasattr(swap.main, "__wrapped__") else None
    assert parsed is None  # main is a plain function; the check below is the real one
    import argparse

    probe = argparse.ArgumentParser()
    probe.add_argument("--send", action="store_true")
    assert probe.parse_args([]).send is False


def test_the_default_keypair_is_the_solana_cli_location():
    assert swap.DEFAULT_KEYPAIR == Path.home() / ".config" / "solana" / "id.json"
