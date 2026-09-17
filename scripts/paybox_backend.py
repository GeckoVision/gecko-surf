"""The PayBox signing backend, in the AUTHORITY role — a key holder that is NOT us.

PayBox (MoonPay) holds the buyer's Solana key in MPC. This file holds two things that
let us ASK it to sign, and nothing else: the user's OAuth bearer and the ``pbxk1.``
signing key that authorises a request headlessly. Neither can move funds on its own,
neither is a wallet key, and neither enters a log, an exception, or a repr. ``gecko/``
still signs nothing; this satisfies :class:`gecko.signer.SigningBackend` from
``scripts/`` exactly as ``privy_backend.py`` and ``os_keychain_backend.py`` do.

HOW IT SIGNS. Through PayBox's own SDK CLI (``@paybox-sh/sdk``): ``paybox --json sign
--credential <id> --intent {op: solanaTransaction, address, transactionBase64}``. With a
signing key configured the SDK completes the request in-process (``autoSign`` defaults
to true) and prints the finished request: ``status: "success"`` and
``output.signedTransactionBase64``. Measured on 2026-09-01 (tx #24): sign at about one
second. The CLI is the vendor's client; re-implementing its request signing in Python
would be a second copy of a protocol we do not own.

THE ROLE. This backend signs as the **authority**: the buyer whose tokens leave. Under a
relay it is slot 1, never slot 0, and it refuses an attestation that names any other
role. PayBox is asked to sign the RELAY's bytes (the message with the Lighthouse
assertion appended); what comes back must carry the buyer's signature over exactly that
message, with the relay's signature still in place. Both are checked here, before the
bytes go back to the seam, because a signer that returns the wrong message or drops a
co-signature would otherwise be caught only at submit, after the blockhash clock ran.

Pattern B: ``run`` is an injected seam (the subprocess call), so every refusal here is
falsifiable offline with no PayBox account and no network (``tests/test_paybox_backend.py``).

Configure from the environment (the SDK's own names; the gecko-app names are accepted
and mapped)::

    PAYBOX_ACCESS_TOKEN or PAYBOX_TOKEN        # OAuth bearer, never logged
    PAYBOX_SIGNING_KEY  or PAYBOX_SIGNIN_KEY   # pbxk1. signing key, never logged
    PAYBOX_CLI                                 # path to the SDK's cli.js (optional)
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

sys.path.insert(0, __file__.rsplit("/scripts/", 1)[0])

from gecko.cosign import CosignRefused, signature_slots, take_signature  # noqa: E402
from gecko.signer import AUTHORITY_ROLE, SigningAttestation  # noqa: E402

#: The one intent this backend issues. Named once.
INTENT_OP = "solanaTransaction"
TIMEOUT_SECONDS = 45

#: Env names the SDK reads, and the gecko-app names that map onto them.
_ENV_ALIASES = {
    "PAYBOX_ACCESS_TOKEN": ("PAYBOX_ACCESS_TOKEN", "PAYBOX_TOKEN"),
    "PAYBOX_SIGNING_KEY": ("PAYBOX_SIGNING_KEY", "PAYBOX_SIGNIN_KEY"),
}
#: Every name a secret may travel under. None of them is forwarded as-is.
_SECRET_ENV = frozenset(name for names in _ENV_ALIASES.values() for name in names)


class PayboxBackendError(Exception):
    """PayBox did not produce a usable signature. Carries a class of failure, never a
    token, a key, or a transaction body."""


#: The subprocess seam: argv -> (exit code, stdout). A fake in a test returns canned JSON.
Run = Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]]


def _default_run(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    completed = subprocess.run(
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stdout


def _cli() -> list[str]:
    """The SDK CLI: ``PAYBOX_CLI``, then ``paybox`` on PATH, then the sibling app's copy."""
    named = os.environ.get("PAYBOX_CLI")
    if named:
        return ["node", named] if named.endswith(".js") else [named]
    on_path = shutil.which("paybox")
    if on_path:
        return [on_path]
    sibling = (
        Path(__file__).resolve().parents[2]
        / "gecko-app/node_modules/@paybox-sh/sdk/dist/cli.js"
    )
    if sibling.exists():
        return ["node", str(sibling)]
    raise PayboxBackendError(
        "the PayBox SDK CLI was not found; set PAYBOX_CLI or install @paybox-sh/sdk"
    )


def _env() -> dict[str, str]:
    """The child's environment: the SDK's names, filled from either alias. Never printed."""
    env = {k: v for k, v in os.environ.items() if k not in _SECRET_ENV}
    for sdk_name, aliases in _ENV_ALIASES.items():
        for alias in aliases:
            value = os.environ.get(alias)
            if value:
                env[sdk_name] = value
                break
    if "PAYBOX_ACCESS_TOKEN" not in env and "PAYBOX_API_KEY" not in os.environ:
        raise PayboxBackendError("no PayBox token in the environment")
    if "PAYBOX_SIGNING_KEY" not in env:
        raise PayboxBackendError(
            "no PayBox signing key in the environment; without it a request parks at "
            "pending_signature and never completes headlessly"
        )
    return env


def _signed_from(result: Mapping[str, Any]) -> str | None:
    out = result.get("output")
    if isinstance(out, Mapping):
        direct = out.get("signedTransactionBase64")
        if isinstance(direct, str):
            return direct
        nested = out.get("value")
        if isinstance(nested, Mapping) and isinstance(
            nested.get("signedTransactionBase64"), str
        ):
            return nested["signedTransactionBase64"]
    return None


@dataclass(frozen=True)
class PayboxWallet:
    credential_id: str
    name: str
    address: str
    approval_mode: str


@dataclass(frozen=True)
class PayboxAuthorityBackend:
    """A :class:`gecko.signer.SigningBackend` that asks PayBox to sign the buyer's slot."""

    wallet: PayboxWallet
    run: Run = field(default=_default_run, repr=False)
    cli: tuple[str, ...] | None = None

    @classmethod
    def open(
        cls,
        *,
        credential: str | None = None,
        run: Run | None = None,
        cli: Sequence[str] | None = None,
    ) -> PayboxAuthorityBackend:
        """List the wallets this token may use, pick one, and require ``autonomous``.

        Fails early and closed: a wallet on ``always_approve`` waits for a passkey on
        every signature, and that wait sits inside the blockhash clock. Refusing at
        configuration time is cheaper than discovering it with verified bytes in hand.
        """
        runner = run or _default_run
        argv = list(cli or _cli())
        code, out = runner([*argv, "--json", "credentials"], _env())
        if code != 0:
            raise PayboxBackendError(f"paybox credentials exited {code}")
        wallets = _wallets(out)
        if credential is not None:
            wallets = [w for w in wallets if w.credential_id == credential]
        if not wallets:
            raise PayboxBackendError(
                "no Solana wallet credential is granted to this token"
            )
        chosen = wallets[0]
        if chosen.approval_mode != "autonomous":
            raise PayboxBackendError(
                f"wallet {chosen.credential_id} is on {chosen.approval_mode!r}; a headless "
                f"signature needs 'autonomous'"
            )
        return cls(wallet=chosen, run=runner, cli=tuple(argv))

    @property
    def pubkey(self) -> str:
        return self.wallet.address

    def sign_transaction(
        self, unsigned_transaction: bytes, attestation: SigningAttestation
    ) -> bytes:
        if attestation.signing_as != AUTHORITY_ROLE:
            raise PayboxBackendError(
                "this backend signs as the authority; it will not sign as fee payer"
            )
        unsigned_b64 = base64.b64encode(unsigned_transaction).decode()
        slots = signature_slots(unsigned_b64)
        if self.pubkey not in slots:
            raise PayboxBackendError(
                "the transaction does not name this wallet as a required signer"
            )
        intent = {
            "op": INTENT_OP,
            "address": self.pubkey,
            "transactionBase64": unsigned_b64,
        }
        argv = [
            *(self.cli or _cli()),
            "--json",
            "sign",
            "--credential",
            self.wallet.credential_id,
            "--intent",
            json.dumps(intent),
        ]
        code, out = self.run(argv, _env())
        if code != 0:
            raise PayboxBackendError(f"paybox sign exited {code}")
        try:
            result = json.loads(out)
        except json.JSONDecodeError:
            raise PayboxBackendError(
                "paybox sign printed something that is not JSON"
            ) from None
        status = result.get("status")
        if status != "success":
            # The status and the request id are public identifiers; the error text is
            # PayBox's and may echo the intent, so it stays out.
            raise PayboxBackendError(
                f"paybox sign ended at status {status!r} (request {result.get('request_id')})"
            )
        signed_b64 = _signed_from(result)
        if not signed_b64:
            raise PayboxBackendError(
                "paybox reported success but returned no signed bytes"
            )

        # WHAT CAME BACK, checked HERE. The seam re-binds the message and reads the slots
        # too; this backend checks first, so a wrong answer is named as PayBox's rather
        # than surfacing as a generic seam refusal.
        try:
            take_signature(
                signed_b64, expect_message=_message_of(unsigned_b64), signer=self.pubkey
            )
        except CosignRefused as exc:
            raise PayboxBackendError(
                f"paybox returned an unusable signature ({exc.code})"
            ) from None
        _cosignatures_intact(unsigned_b64, signed_b64, self.pubkey)
        return base64.b64decode(signed_b64)


def _message_of(tx_b64: str) -> bytes:
    from solders.transaction import Transaction

    return bytes(Transaction.from_bytes(base64.b64decode(tx_b64)).message)


def _cosignatures_intact(unsigned_b64: str, signed_b64: str, own: str) -> None:
    """Every signature that was already present must still be present, byte for byte.

    Under a relay, slot 0 holds the relay's signature when PayBox is asked. A signer that
    hands back a transaction with that slot cleared has produced bytes that need a second
    trip to the relay, inside a blockhash window that is already running.
    """
    from solders.transaction import Transaction

    before = list(Transaction.from_bytes(base64.b64decode(unsigned_b64)).signatures)
    after = list(Transaction.from_bytes(base64.b64decode(signed_b64)).signatures)
    slots = signature_slots(unsigned_b64)
    for name, was, now in zip(slots, before, after):
        if name == own:
            continue
        if bytes(was) != bytes(64) and bytes(was) != bytes(now):
            raise PayboxBackendError(
                f"paybox altered or dropped the co-signature of {name[:8]}…"
            )


def _wallets(out: str) -> list[PayboxWallet]:
    """``credentials --json`` entries may be flat or ``{credential, grant}``-wrapped."""
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        raise PayboxBackendError(
            "paybox credentials printed something that is not JSON"
        ) from None
    items = (
        parsed
        if isinstance(parsed, list)
        else (parsed.get("credentials") or parsed.get("grants") or [])
    )
    wallets: list[PayboxWallet] = []
    for item in items:
        cred = item.get("credential", item) if isinstance(item, Mapping) else {}
        grant = item.get("grant", {}) if isinstance(item, Mapping) else {}
        if cred.get("credential_type") != "wallet":
            continue
        address = (cred.get("metadata") or {}).get("address")
        if not isinstance(address, str):
            continue
        wallets.append(
            PayboxWallet(
                credential_id=str(cred.get("id")),
                name=str(cred.get("name", "")),
                address=address,
                approval_mode=str(
                    grant.get("approval_mode") or cred.get("approval_mode") or "unknown"
                ),
            )
        )
    return wallets
