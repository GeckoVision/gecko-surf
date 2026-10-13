"""Where the hosted any-query reads its secrets: the keychain, and on a host the platform.

Locally every any-query secret comes from the OS keychain alone (``gecko auth set``), on
purpose: never a ``.env`` file. The production host (ECS on Fargate, ``infra/``) has no
keychain. Its secrets live in SSM Parameter Store and ECS injects them into the process
environment at boot through the task definition's ``Secrets:`` ValueFrom, the same path
``MONGODB_URI`` and ``PRIVY_APP_SECRET`` already take for the rest of the server.

So the hosted resolver is the keychain first, then that injected environment, and the
second source exists only when the host says so: ``GECKO_HOST_SECRETS=platform``. Unset,
the resolver is the keychain alone, exactly as before. Any other value is refused rather
than guessed. The platform source reads ``GECKO_CRED_<SLOT>`` for the slots in
:data:`ANYQUERY_SLOTS` only, and treats the SSM sentinel ``__unset__`` as absent.

:func:`ssm_put_request` and :func:`ssm_parameter_name` serve
the operator's push script, which copies a keychain slot into SSM without the
value touching argv, a shell variable, a file or a log.
"""

from __future__ import annotations

import hmac
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping

from ..credentials import (
    ChainResolver,
    CredentialError,
    CredentialRef,
    KeyringBackend,
    PlatformSecretBackend,
)

__all__ = [
    "ANYQUERY_SLOTS",
    "HOST_SECRETS_ENV",
    "HOST_SECRETS_PLATFORM",
    "HostSecretsError",
    "PUSHABLE_SLOTS",
    "compare_slots",
    "push_slots",
    "SSM_PREFIX",
    "anyquery_resolver",
    "ssm_parameter_name",
    "ssm_put_request",
]

HOST_SECRETS_ENV = "GECKO_HOST_SECRETS"
HOST_SECRETS_PLATFORM = "platform"

#: Every keychain slot the hosted any-query reads. ``HELIUS_API_KEY`` is optional (the
#: SOL send falls back to the public endpoint without it); the rest are required.
ANYQUERY_SLOTS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "orthogonal",
        "MONGODB_URI",
        "PRIVY_APP_ID",
        "PRIVY_APP_SECRET",
        "HELIUS_API_KEY",
    }
)

#: What the operator's push script may copy: the secrets above, plus the RPC
#: URL, which can carry a provider key and so is kept in the keychain too.
PUSHABLE_SLOTS = ANYQUERY_SLOTS | {"GECKO_ANYQUERY_RPC_URL"}

#: The server's SSM namespace (``infra/ecs-stack.yml`` grants read on ``/gecko-mcp/*``).
SSM_PREFIX = "/gecko-mcp"


class HostSecretsError(Exception):
    """A refused secret source: an unknown ``GECKO_HOST_SECRETS`` value, or a slot that
    is not pushable or not in the keychain. Names settings and slots, never a value."""


def anyquery_resolver(environ: Mapping[str, str]) -> ChainResolver:
    """The keychain, plus the platform's injected environment when the host flag says so.

    ``environ`` is read for the flag and handed to the platform backend, so a test passes
    a dict and never touches ``os.environ``.
    """
    flag = (environ.get(HOST_SECRETS_ENV) or "").strip()
    if not flag:
        return ChainResolver([KeyringBackend()])
    if flag != HOST_SECRETS_PLATFORM:
        raise HostSecretsError(
            f"{HOST_SECRETS_ENV} must be unset or {HOST_SECRETS_PLATFORM!r}"
        )
    return ChainResolver(
        [KeyringBackend(), PlatformSecretBackend(ANYQUERY_SLOTS, environ=environ)]
    )


def ssm_parameter_name(slot: str) -> str:
    """``/gecko-mcp/<SLOT>``, the parameter ECS maps onto ``GECKO_CRED_<SLOT>``.

    The existing parameters keep their names (``/gecko-mcp/MONGODB_URI``); the slot is
    upper-cased so ``orthogonal`` lands at ``/gecko-mcp/ORTHOGONAL``."""
    return f"{SSM_PREFIX}/{slot.upper()}"


def ssm_put_request(slot: str, value: str) -> str:
    """The ``aws ssm put-parameter --cli-input-json`` body for one slot, as JSON text.

    Written to the CLI's stdin by the caller, so the value is never an argument."""
    return json.dumps(
        {
            "Name": ssm_parameter_name(slot),
            "Value": value,
            "Type": "SecureString",
            "Overwrite": True,
        }
    )


#: ``(argv, stdin) -> (exit code, stdout)``. The script passes ``subprocess.run``; tests a
#: fake. stderr is never surfaced: an AWS error can quote its input.
Runner = Callable[[list[str], str | None], tuple[int, str]]


def _values(slots: Iterable[str], resolver: ChainResolver) -> dict[str, str]:
    """Every slot's value, or :class:`HostSecretsError` naming every slot that is
    missing or not pushable. Resolved in full before anything is written."""
    wanted = list(dict.fromkeys(slots))
    unknown = [slot for slot in wanted if slot not in PUSHABLE_SLOTS]
    if unknown:
        raise HostSecretsError(f"not a pushable slot: {', '.join(unknown)}")
    values: dict[str, str] = {}
    missing: list[str] = []
    for slot in wanted:
        try:
            values[slot] = resolver.resolve(CredentialRef(api=slot))
        except CredentialError:
            missing.append(slot)
    if missing:
        raise HostSecretsError(
            f"not in the keychain: {', '.join(missing)}; set each with "
            "`gecko auth set <NAME>`"
        )
    return values


def push_slots(
    slots: Iterable[str],
    *,
    region: str,
    resolver: ChainResolver,
    run: Runner,
    dry_run: bool = False,
) -> list[str]:
    """Copy each keychain slot to ``/gecko-mcp/<SLOT>`` as a SecureString. One line per
    slot, naming the parameter and the outcome; never a value."""
    lines: list[str] = []
    for slot, value in _values(slots, resolver).items():
        name = ssm_parameter_name(slot)
        if dry_run:
            lines.append(f"would push {name} (from keychain slot {slot!r})")
            continue
        # A 0600 temp file, removed after the call: aws-cli v2 reads file:///dev/stdin
        # from a pipe as empty (measured 10 Oct, exit 252), and an argument would put
        # the value in the process list.
        fd, path = tempfile.mkstemp(prefix="gecko-ssm-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as request:
                request.write(ssm_put_request(slot, value))
            argv = [
                "aws",
                "ssm",
                "put-parameter",
                "--region",
                region,
                "--cli-input-json",
                f"file://{path}",
                "--query",
                "Version",
                "--output",
                "text",
            ]
            code, out = run(argv, None)
        finally:
            os.unlink(path)
        version = out.strip()
        if code == 0 and version.isdigit():
            lines.append(f"pushed {name} (version {version})")
        else:
            lines.append(f"FAILED {name} (aws exit {code})")
    return lines


def compare_slots(
    slots: Iterable[str], *, region: str, resolver: ChainResolver, run: Runner
) -> list[str]:
    """Is each keychain slot the value SSM already holds? ``same`` / ``different`` /
    ``absent``, compared in process; neither value is printed."""
    lines: list[str] = []
    for slot, value in _values(slots, resolver).items():
        name = ssm_parameter_name(slot)
        argv = [
            "aws",
            "ssm",
            "get-parameter",
            "--region",
            region,
            "--name",
            name,
            "--with-decryption",
            "--query",
            "Parameter.Value",
            "--output",
            "text",
        ]
        code, out = run(argv, None)
        if code != 0:
            lines.append(f"{name}: absent or unreadable (aws exit {code})")
            continue
        held = out.rstrip("\n")
        same = hmac.compare_digest(held.encode(), value.encode())
        lines.append(f"{name}: {'same' if same else 'different'}")
        del held
    return lines
