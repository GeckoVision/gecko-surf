"""Log in the way a student does, but keep the key in its own keychain slot.

    uv run python scripts/student_login_test.py --email you@example.com

`gecko login` seals the key into the `gecko-identity` slot, which on a machine that
already has a Gecko identity would replace it. A student's machine has nothing to
replace; the founder's does. This runs the SAME hosted login (email, one-time code,
key) through `gecko.hosted_login.hosted_login`, and changes only two seams: the key is
sealed under `--slot` (default `DEV3PACK_STUDENT_TEST`), and the identity file goes to a
throwaway folder. The code is read with getpass and neither it nor the key is printed.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import tempfile
from pathlib import Path

from gecko import credentials
from gecko.hosted_login import DEFAULT_LOGIN_SERVER, hosted_login
from gecko.login import LoginError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--slot", default="DEV3PACK_STUDENT_TEST")
    parser.add_argument("--server", default=DEFAULT_LOGIN_SERVER)
    args = parser.parse_args(argv)

    slot = credentials.CredentialRef(api=args.slot)

    def store(_ref: credentials.CredentialRef, secret: str) -> bool:
        backend = credentials.KeyringBackend()
        if not backend.available():
            return False
        try:
            backend.store(slot, secret)
        except (credentials.CredentialError, OSError):
            return False
        return True

    with tempfile.TemporaryDirectory(prefix="gecko-student-") as home:
        try:
            hosted_login(
                args.email,
                server_url=args.server,
                prompt=lambda question: getpass.getpass(question),
                store=store,
                home=Path(home),
            )
        except LoginError as error:
            print(f"login failed: {error}", file=sys.stderr)
            return 1
    print(f"key sealed in the keychain as {args.slot} (not printed).")
    print(f"check it: uv run gecko auth test {args.slot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
