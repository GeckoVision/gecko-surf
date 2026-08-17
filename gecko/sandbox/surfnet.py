"""Prove the RPC is a surfnet BEFORE any key material can exist.

WHY THIS MODULE EXISTS. Everything else in Gecko ends at UNSIGNED bytes: the engine
derives, simulates, and hands over. This package is the first code in the repo that may
hold a private key and produce a signature, and the repo's standing rule is that Gecko
never signs or broadcasts a mainnet transaction. A flag (``--fork-only``), an env var,
or a comment are all guards a caller can forget, invert, or lie about. So the guard here
is a TYPE:

    prove_surfnet(rpc_url) -> SurfnetProof        # does the network round-trip
    ephemeral_signer(proof: SurfnetProof)         # the ONLY way to get a key

:func:`ephemeral_signer` takes a *proof*, never a URL. There is no overload, no boolean,
and no default that accepts an endpoint. "Sign without proving" is therefore not a rule
someone must remember — it is a call you cannot write, because the argument does not
exist until a real validator has answered a method mainnet does not implement.

THE PROOF, MEASURED — NOT GUESSED. Recorded against a live
``surfpool start --no-tui --no-deploy --rpc-url <mainnet>`` fork, surfpool 1.1.1
(``solana-core`` 3.1.6), on 2026-08-17::

    --> {"jsonrpc":"2.0","id":1,"method":"surfnet_getSurfnetInfo","params":[]}
    <-- {"jsonrpc": "2.0",
         "id": 1,
         "result": {"context": {"slot": 439792934, "apiVersion": "3.1.6"},
                    "value":   {"runbookExecutions": []}}}

Three things were measured, and each one is load-bearing:

* **The method takes NO parameters.** Sending ``[{}]`` — the shape most Solana RPC
  methods accept as a trailing config object — is rejected with
  ``code=-32602 message='Invalid parameters: No parameters were expected'``. So the
  params list is fixed at ``[]`` here rather than exposed as a knob.
* **An unknown method answers ``-32601 'Method not found'``.** That is the code a
  non-surfnet (mainnet, or any stock Agave RPC) returns for ``surfnet_getSurfnetInfo``,
  and it is what :class:`NotASurfnetError` is raised on.
* **``getGenesisHash`` is NOT a discriminator.** The fork returns mainnet's own genesis
  hash (``5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d``), because it *is* a fork of
  mainnet. Anyone reaching for genesis to tell the two apart gets a false positive; the
  cheatcode method is the only honest signal. (``getVersion`` does carry an extra
  ``surfnet-version`` key, but it is corroboration, not the gate.)

``result.value`` is thin — one ``runbookExecutions`` list — and that is exactly what is
checked, together with a live integer ``context.slot``. A stock RPC has no notion of a
runbook, so the field's mere presence is the surfnet's own signature. If a future
surfpool renames it, this refuses to hand out a key rather than degrading: for a module
whose failure mode is "signed against mainnet", fail-closed is the only safe direction.

WHAT THE KEY IS, AND IS NOT. :class:`EphemeralSigner` holds a freshly generated
``solders`` keypair in memory for the life of the object. It is never written to disk,
never logged, never serialised, never returned, and never rendered — ``repr``/``str``
show the public key and the word ``redacted``. Every exception this module raises is
passed through :func:`_redact` first, which strips long base58/hex/base64 runs by
pattern, so a stray upstream error message cannot carry secret bytes out. Python cannot
guarantee the bytes are erased from the heap when the object dies; that limitation is
stated rather than papered over, and it is why the key is single-use, unfunded, and
bound to a proven local fork.

Control-plane invariant #1 holds: nothing here is persisted. The proof carries only
public validator metadata, and the key never leaves the process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping
from weakref import WeakSet

from ..rpc import RpcCall, default_rpc_call, validate_rpc_url

if (
    TYPE_CHECKING
):  # solders is the [solana] extra — keep import-time cost off the engine
    from solders.keypair import Keypair

__all__ = [
    "SURFNET_INFO_METHOD",
    "EphemeralSigner",
    "EphemeralSignerError",
    "NotASurfnetError",
    "SandboxError",
    "SurfnetProof",
    "ephemeral_signer",
    "prove_surfnet",
]

#: The cheatcode a surfnet implements and mainnet does not. This is the whole gate.
SURFNET_INFO_METHOD = "surfnet_getSurfnetInfo"

#: Measured: the method rejects ANY params, including a trailing config object.
_NO_PARAMS: list[Any] = []


class SandboxError(Exception):
    """Base for every refusal in :mod:`gecko.sandbox`. Messages carry public metadata
    only — never key bytes, never a request body."""


class NotASurfnetError(SandboxError):
    """The endpoint did not prove it is a local surfnet, so no key was created.

    Raised when :data:`SURFNET_INFO_METHOD` is missing (JSON-RPC ``-32601``), errors,
    is unreachable, or answers in a shape that is not a surfnet's. Always names the URL,
    because the overwhelmingly likely cause is a misconfigured endpoint and the operator
    needs to see WHICH one was refused.
    """


class EphemeralSignerError(SandboxError):
    """The signer was asked to do something that would expose or persist key material,
    or was handed a message it will not sign."""


def _redact(text: str) -> str:
    """Strip anything shaped like encoded key material from a message, by PATTERN.

    Not a comparison against the actual secret: comparing would mean materialising the
    secret into a string in order to search for it, which is the leak it is meant to
    prevent. Instead every long base58 / hex / base64 run is replaced outright. This is
    deliberately over-eager — a redacted diagnostic is a cost, a leaked seed is not.
    A 32-byte value is 43-44 base58 chars, 64 hex chars, or 44 base64 chars; the
    thresholds sit below all three so a partial or padded encoding is caught too.
    """
    redacted = re.sub(r"\b[0-9a-fA-F]{40,}\b", "<redacted>", text)
    redacted = re.sub(r"\b[1-9A-HJ-NP-Za-km-z]{40,}\b", "<redacted>", redacted)
    redacted = re.sub(r"\b[A-Za-z0-9+/]{40,}={0,2}", "<redacted>", redacted)
    # A keypair spilled as a Python/JSON byte list: [12, 240, 3, ...]. Any bracketed run
    # of >=16 small integers is far more likely a secret than a legitimate diagnostic.
    return re.sub(r"\[(?:\s*\d{1,3}\s*,){16,}[^\]]*\]", "<redacted>", redacted)


def _surfnet_evidence(result: Any, rpc_url: str) -> Mapping[str, Any]:
    """Return ``result`` iff it is the answer a surfnet gives, else raise.

    Every branch is a refusal rather than a coercion. The two facts demanded are a live
    integer ``context.slot`` (a running validator, not a canned blob) and a
    ``value.runbookExecutions`` list (a concept that exists only in surfpool).

    Run on the raw RPC answer AND again inside :meth:`SurfnetProof.__post_init__` on the
    deep-frozen copy, so the sequence check must accept a tuple as well as a list — the
    freeze is what turns one into the other, and a check that only knew ``list`` would
    reject the very proof this module produced.
    """
    if not isinstance(result, Mapping):
        raise NotASurfnetError(
            f"{rpc_url} answered {SURFNET_INFO_METHOD} with "
            f"{type(result).__name__}, not an object — refusing to treat it as a surfnet"
        )
    context = result.get("context")
    if not isinstance(context, Mapping) or not isinstance(context.get("slot"), int):
        raise NotASurfnetError(
            f"{rpc_url} answered {SURFNET_INFO_METHOD} without a live integer "
            f"'context.slot' — refusing to treat it as a surfnet"
        )
    value = result.get("value")
    if not isinstance(value, Mapping) or not isinstance(
        value.get("runbookExecutions"), (list, tuple)
    ):
        raise NotASurfnetError(
            f"{rpc_url} answered {SURFNET_INFO_METHOD} without "
            f"'value.runbookExecutions' — refusing to treat it as a surfnet "
            f"(measured against surfpool 1.1.1; a rename fails CLOSED on purpose)"
        )
    return result


def _freeze(value: Any) -> Any:
    """Deep-freeze the proof payload so a held proof cannot be edited after the check."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


class _Attestation:
    """An opaque token :func:`prove_surfnet` mints for ONE url it actually reached.

    Compared by IDENTITY, never by value, and constructible only from inside this
    module. That is the whole mechanism: no amount of copying, replacing or
    hand-authoring a :class:`SurfnetProof` produces one of these, because producing one
    requires having run the round trip.
    """

    # __weakref__ so _MINTED can hold these weakly — a proof going out of scope must
    # not keep its attestation alive in a module-level set forever.
    __slots__ = ("rpc_url", "__weakref__")

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return f"<surfnet attestation for {self.rpc_url}>"


#: Every attestation this module actually minted, held weakly and checked by IDENTITY.
#: The leading underscore on `_Attestation` is a convention, not a wall — an in-repo
#: caller can import it and construct one. Membership here cannot be forged that way:
#: getting in requires having gone through `prove_surfnet`, which requires the round trip.
_MINTED: "WeakSet[_Attestation]" = WeakSet()


@dataclass(frozen=True)
class SurfnetProof:
    """What the fork said about itself — the only ticket :func:`ephemeral_signer` takes.

    WHY AN ATTESTATION AND NOT JUST RE-VALIDATION. This class used to defend itself by
    re-running the evidence check in ``__post_init__``, on the theory that forging a
    proof would then require hand-writing a payload impersonating a surfnet. That was
    wrong, and an adversarial pass proved it by signing a transaction aimed at mainnet:

        dataclasses.replace(real_proof, rpc_url="https://api.mainnet-beta.solana.com")

    re-runs the check and PASSES, because it carries the genuine evidence a real fork
    returned and swaps one string. Nothing was hand-written. `object.__setattr__`, a
    subclass overriding ``__post_init__``, and a hand-built payload all worked too. The
    evidence was never tied to the URL it came from, and the downstream bindings could
    not catch it because every one of them compares against ``proof.rpc_url`` itself —
    internal consistency, never authenticity.

    What actually prevented harm was step ordering: funding runs before signing and
    mainnet answers ``-32601`` to a cheatcode. That is luck wearing a safety
    property's clothes, and no test protected it.

    So the ticket is now an unforgeable :class:`_Attestation`, minted inside
    :func:`prove_surfnet` for the exact url that answered, checked by identity, and
    ``replace()`` is refused outright.
    """

    #: The endpoint that proved itself. Downstream code binds its submit URL to THIS.
    rpc_url: str
    #: The raw ``result`` of :data:`SURFNET_INFO_METHOD`, deep-frozen. Public metadata.
    detail: Mapping[str, Any] = field(default_factory=dict)
    #: The unforgeable half. Absent on any proof this module did not mint.
    attestation: _Attestation | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_rpc_url(self.rpc_url)
        _surfnet_evidence(self.detail, self.rpc_url)

    def __replace__(self, /, **changes: Any) -> "SurfnetProof":
        """Refuse `dataclasses.replace` — it was the shortest path to a mainnet key.

        Python 3.13+ routes ``dataclasses.replace`` through this method; on 3.11/3.12 it
        rebuilds via ``__init__``, which cannot supply an attestation from outside this
        module either. Both paths are therefore closed, and this one says why.
        """
        raise NotASurfnetError(
            "a surfnet proof cannot be copied with changes — the attestation is bound "
            "to the endpoint that answered. Call prove_surfnet() against the endpoint "
            "you actually mean to use."
        )

    @property
    def slot(self) -> int:
        """The validator slot at the moment of proof. Present by construction."""
        context = self.detail["context"]
        assert isinstance(context, Mapping)  # guaranteed by _surfnet_evidence
        slot = context["slot"]
        assert isinstance(slot, int)
        return slot


def prove_surfnet(rpc_url: str, rpc_call: RpcCall | None = None) -> SurfnetProof:
    """Make the endpoint prove it is a local surfnet. No key exists on either path.

    Calls :data:`SURFNET_INFO_METHOD` with no params and validates the answer's shape.
    Every failure of the round-trip — unreachable host, JSON-RPC ``-32601 Method not
    found``, any other RPC error, or an answer that is not a surfnet's — becomes
    :class:`NotASurfnetError` naming ``rpc_url``. A non-http ``rpc_url`` raises
    :class:`~gecko.rpc.RpcError` from the shared scheme gate BEFORE the transport runs;
    that stays the canonical transport type rather than being re-wrapped, because it is
    a fact about the URL and not about the endpoint's identity. Both are refusals and
    neither reaches key material — which is the entire point: a misconfigured or hostile
    RPC is refused BEFORE a secret has been brought into existence, not after.

    ``rpc_call`` is injectable so the refusal branch is falsifiable offline with a light
    fake transport — no mainnet call is ever needed to test the guard.
    """
    validate_rpc_url(rpc_url)
    call = rpc_call or default_rpc_call
    try:
        response = call(rpc_url, SURFNET_INFO_METHOD, list(_NO_PARAMS))
    except NotASurfnetError:
        raise
    except Exception as exc:
        # Fail closed on EVERY transport outcome. An RPC that cannot answer the
        # cheatcode has not proved anything, whatever the reason.
        raise NotASurfnetError(
            f"{rpc_url} did not answer {SURFNET_INFO_METHOD} "
            f"({type(exc).__name__}: {_redact(str(exc))}) — refusing to create a key "
            f"for an endpoint that has not proved it is a local surfnet"
        ) from None
    if not isinstance(response, Mapping):
        raise NotASurfnetError(
            f"{rpc_url} returned a non-object JSON-RPC response to "
            f"{SURFNET_INFO_METHOD} — refusing to treat it as a surfnet"
        )
    evidence = _surfnet_evidence(response.get("result"), rpc_url)
    attestation = _Attestation(rpc_url)
    _MINTED.add(attestation)
    return SurfnetProof(
        rpc_url=rpc_url, detail=_freeze(evidence), attestation=attestation
    )


def _generate_keypair() -> Keypair:
    """The ONE place a private key comes into existence in this repo.

    Kept as a named seam so the ordering guarantee is testable: a test can record when
    this is called and assert the RPC proof happened first. Reordering the two — proving
    after generating, or generating inside :func:`prove_surfnet` — flips that recorded
    order and turns the test red.
    """
    from solders.keypair import Keypair

    return Keypair()


class EphemeralSigner:
    """A single-use keypair that exists only in memory, only for a proven surfnet.

    Exposes the public key and a raw ``sign``. Nothing returns the secret, and every
    escape hatch Python offers for getting it out — ``repr``, ``str``, pickling,
    copying, ``vars()`` via ``__dict__`` — is closed or redacted below.

    HONEST LIMIT: Python cannot guarantee the secret's bytes are wiped from the heap
    when this object is collected. That is why the key is generated per call, never
    funded beyond a local faucet, and bound to :attr:`rpc_url` — a proven local fork.
    """

    # No __dict__: `vars(signer)` raises instead of handing back the keypair, and no
    # caller can stash a second reference to the secret on the instance.
    __slots__ = ("_keypair", "_pubkey", "_rpc_url")

    def __init__(self, proof: SurfnetProof) -> None:
        if not isinstance(proof, SurfnetProof):
            raise EphemeralSignerError(
                f"a surfnet proof is required to create a signer, got "
                f"{type(proof).__name__} — no key was created"
            )
        attestation = proof.attestation
        # Identity, not equality: a value comparison would accept anything shaped like
        # an attestation, and the point is that only prove_surfnet can make one.
        if (
            not isinstance(attestation, _Attestation)
            or attestation not in _MINTED
            or attestation.rpc_url is not proof.rpc_url
        ):
            raise EphemeralSignerError(
                "this surfnet proof carries no attestation for its own rpc_url, so the "
                "endpoint was never made to prove itself — no key was created. A proof "
                "must come from prove_surfnet(), not from replace(), setattr, a "
                "subclass, or a hand-built payload."
            )
        self._rpc_url = proof.rpc_url
        self._keypair = _generate_keypair()
        # Cached so `pubkey` never has to reach through the keypair on a hot path, and
        # so a rendered signer is one string lookup away from never touching the secret.
        self._pubkey = str(self._keypair.pubkey())

    @property
    def pubkey(self) -> str:
        """The base58 public key. Safe to log, print, and fund on the fork."""
        return self._pubkey

    @property
    def rpc_url(self) -> str:
        """The surfnet this key was proven against. Bind any submit URL to this."""
        return self._rpc_url

    def sign(self, message_bytes: bytes) -> bytes:
        """Sign ``message_bytes`` and return the 64-byte signature.

        Any upstream failure is re-raised as :class:`EphemeralSignerError` with the
        message run through :func:`_redact`, so a signing library that echoes its input
        (or its key) into an error string cannot carry those bytes out of here.
        """
        if not isinstance(message_bytes, (bytes, bytearray)):
            raise EphemeralSignerError(
                f"sign() takes raw bytes, got {type(message_bytes).__name__}"
            )
        if not message_bytes:
            raise EphemeralSignerError("refusing to sign an empty message")
        try:
            signature = self._keypair.sign_message(bytes(message_bytes))
        except Exception as exc:
            raise EphemeralSignerError(
                f"signing failed ({type(exc).__name__}: {_redact(str(exc))})"
            ) from None
        return bytes(signature)

    def __repr__(self) -> str:
        return f"EphemeralSigner(pubkey={self._pubkey}, secret=<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise EphemeralSignerError(
            "an EphemeralSigner is never serialised — the key must not leave memory"
        )

    def __getstate__(self) -> Any:
        raise EphemeralSignerError(
            "an EphemeralSigner is never serialised — the key must not leave memory"
        )

    def __copy__(self) -> Any:
        raise EphemeralSignerError("an EphemeralSigner is never copied")

    def __deepcopy__(self, _memo: Any) -> Any:
        raise EphemeralSignerError("an EphemeralSigner is never copied")


def ephemeral_signer(proof: SurfnetProof) -> EphemeralSigner:
    """Create an in-memory keypair for an endpoint that has ALREADY proved itself.

    The parameter is a :class:`SurfnetProof`, not a URL, on purpose: the proof is the
    only object that carries "a validator answered the surfnet cheatcode", and it cannot
    be constructed without that answer. So the unsafe program — generate a key, then
    point it at whatever endpoint was configured — has no spelling in this API.
    """
    return EphemeralSigner(proof)
