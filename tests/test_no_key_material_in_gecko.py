"""The custody invariant, enforced over the AST: no key material in ``gecko/``.

``scripts/sign_and_send.py:1-5`` states the promise this file mechanises — "nothing in
``gecko/`` signs, holds a key, or broadcasts, and that stays true". Autonomous signing is
exactly the change that would quietly make that sentence false, so the sentence gets a
test rather than a docstring.

WHY THE AST AND NOT A GREP. The lint tests already in this suite read source text and
match substrings, which any author defeats without meaning to: ``"key" + "_path"`` is not
the token ``key_path``, a parameter split across a line continuation is not one line, and
a comment mentioning a banned word is a false positive that trains people to loosen the
pattern. Parsing gives us the thing we actually care about — the *signature* — and lets
us ask two different questions of it: does a public function ACCEPT key material, and
does a public function RETURN it.

WHAT IT DOES NOT PROVE. A signature check is static; it says nothing about the VALUE a
function returns at runtime. ``credentials.py``'s backend contract is
``get(ref) -> str | None`` and that ``str`` is the secret — a perfectly clean signature
carrying material. That runtime half is asserted separately, at the one seam where a
signing key is resolved, in ``tests/test_signer.py::test_key_handle_is_opaque``. Neither
test subsumes the other and neither is sufficient alone.

The checker is itself falsified below (``test_checker_flags_a_synthetic_violation``): a
guard that has never fired once is a guard nobody knows is wired.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

GECKO = Path(__file__).resolve().parents[1] / "gecko"

#: Normalised parameter/field names that would mean this process holds key material.
#: Normalisation strips punctuation and case, so ``secret_key``, ``secretKey`` and
#: ``SECRET-KEY`` are one token and cannot be spelled around.
#:
#: ``secret`` alone is deliberately NOT here: ``KeyringBackend.store(ref, secret)`` writes
#: a provider API credential to the OS keychain, which is this module's job and not key
#: material. The ban is on SIGNING key material specifically — the thing whose possession
#: would make Gecko a custody provider.
BANNED_PARAMS = frozenset(
    {
        "keypair",
        "keypairpath",
        "keypairfile",
        "keypath",
        "keyfile",
        "keybytes",
        "mnemonic",
        "seedphrase",
        "secretkey",
        "secretbytes",
        "privatekey",
        "privkey",
        "signingkey",
        "signerkey",
        "idjson",
    }
)

#: Type names that, if they appear anywhere in a public RETURN annotation, mean the
#: function hands key material back to its caller.
BANNED_RETURN_TYPES = frozenset(
    {"Keypair", "SigningKey", "SecretKey", "PrivateKey", "Mnemonic", "SeedPhrase"}
)


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _is_public(name: str) -> bool:
    """Dunders are public API; a single leading underscore is not."""
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


def _annotation_names(node: ast.expr | None) -> set[str]:
    """Every bare identifier mentioned in an annotation, however nested.

    ``dict[str, Keypair]`` and ``Keypair | None`` both surface ``Keypair``; a string
    annotation (``-> "Keypair"``) is re-parsed rather than trusted as opaque text.
    """
    if node is None:
        return set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return set()
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def custody_violations(source: str, filename: str) -> list[str]:
    """Every public signature in ``source`` that takes or returns signing key material.

    Returns human-readable lines; an empty list is the invariant holding.
    """
    violations: list[str] = []
    tree = ast.parse(source, filename)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_public(node.name):
                continue
            args = node.args
            every = [
                *args.posonlyargs,
                *args.args,
                *args.kwonlyargs,
                args.vararg,
                args.kwarg,
            ]
            for arg in every:
                if arg is None:
                    continue
                if _normalise(arg.arg) in BANNED_PARAMS:
                    violations.append(
                        f"{filename}:{node.lineno}: {node.name}() takes key material "
                        f"parameter {arg.arg!r}"
                    )
                bad = _annotation_names(arg.annotation) & BANNED_RETURN_TYPES
                if bad:
                    violations.append(
                        f"{filename}:{node.lineno}: {node.name}() takes key material "
                        f"typed {sorted(bad)} via {arg.arg!r}"
                    )
            returned = _annotation_names(node.returns) & BANNED_RETURN_TYPES
            if returned:
                violations.append(
                    f"{filename}:{node.lineno}: {node.name}() returns key material "
                    f"{sorted(returned)}"
                )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            # A dataclass/class attribute is a carrier just as much as a parameter is:
            # `secret_key: bytes` on a frozen dataclass stores material and hands it to
            # every reader of the instance.
            name = node.target.id
            if _is_public(name) and _normalise(name) in BANNED_PARAMS:
                violations.append(
                    f"{filename}:{node.lineno}: attribute {name!r} carries key material"
                )
            bad_field = _annotation_names(node.annotation) & BANNED_RETURN_TYPES
            if _is_public(name) and bad_field:
                violations.append(
                    f"{filename}:{node.lineno}: attribute {name!r} is typed "
                    f"{sorted(bad_field)}"
                )
    return violations


def _gecko_modules() -> list[Path]:
    modules = sorted(GECKO.rglob("*.py"))
    assert modules, "no gecko/*.py found — the guard would pass vacuously"
    return modules


def test_no_public_signature_in_gecko_takes_or_returns_key_material() -> None:
    """The invariant itself, over every module in the package."""
    violations: list[str] = []
    for path in _gecko_modules():
        violations.extend(
            custody_violations(path.read_text(encoding="utf-8"), str(path))
        )
    assert not violations, "custody invariant broken:\n" + "\n".join(violations)


def test_the_signer_module_is_actually_scanned() -> None:
    """The seam this guard exists for must be inside the scanned set.

    Without this, deleting ``gecko/signer.py`` — or writing it somewhere the glob does
    not reach — turns the test above green for the wrong reason.
    """
    scanned = {p.name for p in _gecko_modules()}
    assert "signer.py" in scanned
    assert "credentials.py" in scanned


@pytest.mark.parametrize(
    "snippet",
    [
        "def sign(keypair_path: str) -> None: ...",
        "def sign(*, mnemonic: str) -> None: ...",
        "def load() -> Keypair: ...",
        "def load() -> 'Keypair | None': ...",
        "def sign(k: Keypair) -> None: ...",
        "class S:\n    secret_key: bytes\n",
        "def sign(**secret_key: bytes) -> None: ...",
    ],
)
def test_checker_flags_a_synthetic_violation(snippet: str) -> None:
    """The guard has to be able to fail, or it proves nothing about the code it scans."""
    assert custody_violations(snippet, "<synthetic>")


@pytest.mark.parametrize(
    "snippet",
    [
        "def store(ref: str, secret: str) -> None: ...",  # API credential, not a key
        "def _sign(keypair_path: str) -> None: ...",  # private helper is out of scope
        "def resolve() -> KeyHandle: ...",  # an opaque handle is the whole point
        "def pubkey() -> str: ...",
    ],
)
def test_checker_does_not_fire_on_legitimate_signatures(snippet: str) -> None:
    """Over-firing is how a guard gets loosened until it means nothing."""
    assert not custody_violations(snippet, "<synthetic>")


def test_only_one_signer_name_exists_in_the_package() -> None:
    """C3: ``access.py``'s ``Signer`` is the auth-handshake callable and keeps the name.

    Two different things called ``Signer`` in one package is how a reviewer reads the
    wrong contract at the moment it matters most. The transaction seam is
    ``TransactionSigner``.
    """
    declarations: list[str] = []
    for path in _gecko_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "Signer":
                declarations.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "Signer":
                        declarations.append(f"{path.name}:{node.lineno}")
    assert len(declarations) == 1, f"expected exactly one 'Signer', got {declarations}"
    assert declarations[0].startswith("access.py:")


def test_no_module_in_gecko_imports_a_keypair_type() -> None:
    """C13/custody: the package may not so much as NAME the type that holds a secret key.

    Deliberately an import check rather than a text search for ``sendTransaction``. That
    search fires on seven modules today and every one is innocent: ``simulate.py:11`` and
    ``landing.py:15`` assert the negative in prose, and ``jito_surface.py:68`` carries
    ``sendTransaction`` as a CATALOG IDENTIFIER for an operation we specifically refuse to
    serve live. A guard whose failures are all false positives is a guard someone deletes,
    so this asks the question with no innocent answer: an import of ``solders.keypair`` is
    the first line of becoming a custody provider, and there is none today.

    ``scripts/sign_and_send.py`` imports it, inside a function, on purpose — that file is
    the founder's hand on the pen and is not scanned here.
    """
    offenders: list[str] = []
    for path in _gecko_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "solders.keypair"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("solders.keypair"):
                        offenders.append(f"{path.name}:{node.lineno}")
            # `Keypair` reached by any other route counts too.
            if isinstance(node, ast.Name) and node.id == "Keypair":
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "Keypair":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"gecko/ must never hold a keypair: {offenders}"
