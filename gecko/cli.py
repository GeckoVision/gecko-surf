"""``gecko`` CLI — an argparse subcommand dispatcher. Thin by design.

Three verbs, each a thin wrapper over the package (all real logic lives in the
engine modules):

  * ``gecko serve <spec>``      comprehend an OpenAPI spec and serve it to agents (MCP)
  * ``gecko test <spec>``       generate + run first-call-correctness checks (testgen)
  * ``gecko from-docs <src>``   recover a draft OpenAPI from a doc page, then comprehend

Backward-compat: a bare ``gecko <spec> [flags]`` (no subcommand) still comprehends +
serves, identically to before — the dispatcher defaults an unrecognized first token
to ``serve``. ``python -m gecko.serve`` also keeps working unchanged.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from . import credentials, doctor, docs_reader, serve, testgen
from .access import public_session
from .client import AgentApiClient
from .netguard import UnsafeUrlError, validate_public_url

_SUBCOMMANDS = ("serve", "test", "from-docs", "auth", "doctor")
# Below this many recovered ops we hint that agent-browser renders JS nav better.
_FEW_OPS = 2


def _default_to_serve(argv: list[str]) -> tuple[str, list[str]]:
    """Split argv into (command, rest), defaulting the legacy bare form to ``serve``.

    ``gecko <spec>`` (no subcommand) must behave exactly like ``gecko serve <spec>``,
    so anything that isn't a known subcommand token or a bare help flag is treated as
    the first positional of ``serve``.
    """
    if not argv:
        return "help", []
    head = argv[0]
    if head in _SUBCOMMANDS:
        return head, argv[1:]
    if head in ("-h", "--help"):
        return "help", []
    return "serve", argv


def _reject_unsafe(url: str, verb: str) -> bool:
    """Early, friendly SSRF check for http(s) inputs. True => refuse (already logged)."""
    if not url.startswith(("http://", "https://")):
        return False
    try:
        validate_public_url(url)
    except UnsafeUrlError as exc:
        print(f"Refusing to {verb} unsafe URL: {exc}", file=sys.stderr)
        return True
    return False


def _cmd_test(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="gecko test",
        description="Generate + run first-call-correctness checks for an API.",
    )
    p.add_argument("spec", help="OpenAPI 3.x URL (or local path for dev).")
    p.add_argument(
        "--mode",
        choices=("recorded", "live"),
        default="recorded",
        help="recorded ($0, synthesized) or live (real upstream calls).",
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="Also write a standalone pytest module here (commit it to CI).",
    )
    args = p.parse_args(argv)

    if _reject_unsafe(args.spec, "ingest"):
        return 2
    try:
        results = testgen.check(args.spec, mode=args.mode)
    except (UnsafeUrlError, ValueError) as exc:
        print(f"Could not comprehend spec: {exc}", file=sys.stderr)
        return 2

    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.tool} · {r.kind} — {r.detail}")
    passed, total = testgen.summary(results)
    print(f"\n{passed}/{total} checks passed ({args.mode} mode)")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(testgen.render_module(args.spec, out_name=args.out))
        print(f"wrote pytest module -> {args.out}")

    return 0 if passed == total else 1


def _cmd_from_docs(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="gecko from-docs",
        description="Recover a draft OpenAPI from a doc page, then comprehend it.",
    )
    p.add_argument("source", help="Doc-site URL (or local HTML path for dev).")
    p.add_argument(
        "-o", "--out", default=None, help="Write the draft OpenAPI JSON here."
    )
    p.add_argument(
        "--name", default=None, help="Draft title (default: the page's first heading)."
    )
    args = p.parse_args(argv)

    if _reject_unsafe(args.source, "fetch"):
        return 2
    try:
        result = docs_reader.from_docs(args.source, title=args.name)
    except (UnsafeUrlError, OSError, ValueError) as exc:
        print(f"Could not read docs: {exc}", file=sys.stderr)
        return 2

    ops = result.ops
    print("Gecko from-docs — recover a draft API from human docs\n" + "=" * 56)
    print(f"source:    {result.source}")
    print(f"recovered {len(ops)} candidate operation(s):")
    for op in ops:
        print(
            f"  - {op.operation_id}  [{op.http_method} {op.http_path}]  "
            f"({op.transport}, {op.confidence})"
        )
    print(
        f"\nhonesty: {result.review_notes} x-review note(s), "
        f"{result.low_confidence} low/medium-confidence field(s) to confirm."
    )
    if result.uuid_auth:
        print(
            f"optional auth recovered: {result.uuid_auth['name']} header "
            "(injected by the access layer, invisible to the agent)."
        )

    if len(ops) < _FEW_OPS:
        print(
            "\nNote: stdlib fetch recovered few operations — this doc may render its "
            "API nav with JavaScript.\nThe spikes/docs_reader agent-browser driver "
            "renders JS-rendered nav better (optional, not required):\n"
            "  uv run python -m spikes.docs_reader.driver <docs-url> --out draft.json"
        )

    # Comprehend the draft through the UNMODIFIED engine — the honest end-to-end.
    client = AgentApiClient(result.draft, session=public_session())
    tools = client.list_tools()
    print(f"\ncomprehended draft -> {len(tools)} agent tool(s):")
    for t in tools:
        print(f"  - {t['name']}: {t['description']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result.draft, fh, indent=2)
        print(f"\nwrote draft OpenAPI -> {args.out}")

    return 0


def _cmd_auth(argv: list[str]) -> int:
    """`gecko auth set|rm|list` — thin transport over ``credentials`` (keychain).

    All keychain logic lives in ``gecko.credentials``; this only parses args,
    reads the secret via a HIDDEN prompt (never argv/history), and formats output.
    """
    p = argparse.ArgumentParser(
        prog="gecko auth",
        description="Hold your provider key in the OS keychain (never a dotfile).",
    )
    sub = p.add_subparsers(dest="action")

    p_set = sub.add_parser("set", help="Store a provider secret (hidden prompt).")
    p_set.add_argument("api", help="Surface/provider name, e.g. colosseum.")
    p_set.add_argument("--account", default=None, help="Named identity (optional).")
    p_set.add_argument(
        "--scheme",
        choices=("raw", "bearer"),
        default="raw",
        help="How the value renders at call time (control-plane mapping).",
    )

    p_rm = sub.add_parser("rm", help="Delete a keychain credential (idempotent).")
    p_rm.add_argument("api")
    p_rm.add_argument("--account", default=None)

    sub.add_parser("list", help="List stored credential NAMES (never a value).")

    p_test = sub.add_parser(
        "test", help="Resolve a credential; report the backend only (never a value)."
    )
    p_test.add_argument("api", help="Surface/provider name, e.g. colosseum.")
    p_test.add_argument("--account", default=None, help="Named identity (optional).")

    args = p.parse_args(argv)
    if args.action == "set":
        return _auth_set(args.api, args.account, args.scheme)
    if args.action == "rm":
        return _auth_rm(args.api, args.account)
    if args.action == "list":
        return _auth_list()
    if args.action == "test":
        return _auth_test(args.api, args.account)
    p.print_help()
    return 0


def _auth_set(api: str, account: str | None, scheme: str) -> int:
    ref = credentials.CredentialRef(api=api, account=account)
    backend = credentials.KeyringBackend()
    if not backend.available():
        # REFUSE — never write plaintext anywhere; print the fallbacks instead.
        print(
            "No OS keychain available (install it: pip install "
            "'gecko-surf[credentials]').",
            file=sys.stderr,
        )
        print(
            f"Use the env fallback instead:\n  export "
            f"{credentials.env_var_name(ref)}=...",
            file=sys.stderr,
        )
        return 1
    # getpass keeps the value out of argv (/proc/cmdline, ps), history, scrollback.
    secret = getpass.getpass(f"Enter secret for {ref.slot()} (input hidden): ")
    if not secret:
        print("No secret entered; nothing stored.", file=sys.stderr)
        return 1
    backend.store(ref, secret)
    print(f"Stored {ref.slot()} in the OS keychain.")
    # --scheme is the surface's control-plane render hint; there is no config store
    # in Phase 2, so it is not persisted here — the live session supplies it.
    print(f"Render scheme at call time: {scheme} (supplied by the surface mapping).")
    return 0


def _auth_rm(api: str, account: str | None) -> int:
    ref = credentials.CredentialRef(api=api, account=account)
    backend = credentials.KeyringBackend()
    if not backend.available():
        print("No OS keychain available; nothing to remove.")
        return 0  # idempotent
    existed = backend.delete(ref)
    if existed:
        print(f"Removed {ref.slot()} from the keychain.")
    else:
        print(f"No keychain entry for {ref.slot()} (nothing to remove).")
    return 0


def _auth_list() -> int:
    backend = credentials.KeyringBackend()
    resolver = credentials.default_resolver()
    printed = False
    if backend.available():
        for slot in backend.list_slots():
            ref = credentials.ref_from_slot(slot)
            who = credentials.which_backend(ref, resolver) or "keyring"
            print(f"  {slot}  ({who})")
            printed = True
    for name in credentials.env_visible_names():
        print(f"  {name}  (env)")
        printed = True
    if not printed:
        print("No stored credentials. Add one:  gecko auth set <api>")
    return 0


def _auth_test(api: str, account: str | None) -> int:
    """Resolve the credential and report ONLY which backend answered — never the
    value, its length, or a prefix. ``which_backend`` reads the value internally to
    confirm a non-empty hit but never returns or logs it."""
    ref = credentials.CredentialRef(api=api, account=account)
    resolver = credentials.default_resolver()
    try:
        who = credentials.which_backend(ref, resolver)
    except credentials.CredentialError as exc:
        # A configured command that failed: the error carries name + exit code only.
        print(f"auth: {exc}", file=sys.stderr)
        return 1
    if who is None:
        print(credentials.no_credential_message(ref), file=sys.stderr)
        return 1
    print(f"resolved ✓ via {who}")
    return 0


def _cmd_doctor(argv: list[str]) -> int:
    """`gecko doctor [api] [--json] [--remote]` — read-only setup diagnosis.

    Thin: parse args, call ``doctor.run_doctor``, format. Prints a human table by
    default; ``--json`` emits the ``DoctorReport`` so an agent can read + act on it.
    Never edits config, writes files, or touches the network.
    """
    p = argparse.ArgumentParser(
        prog="gecko doctor",
        description="Diagnose the local MCP setup and print the exact add command.",
    )
    p.add_argument(
        "api",
        nargs="?",
        default=None,
        help="Surface/provider name to presence-probe a credential for (optional).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the DoctorReport as JSON (for an agent to consume and act on).",
    )
    p.add_argument(
        "--remote",
        action="store_true",
        help="Diagnose for a genuinely remote/sandboxed client: recommend HTTP and "
        "probe cloudflared (the --tunnel fallback). Default is stdio (no tunnel).",
    )
    args = p.parse_args(argv)

    report = doctor.run_doctor(args.api, remote=args.remote)
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(doctor.render_text(report))
    return 0


def _print_help() -> None:
    print("gecko — make any API agent-usable without integration code\n")
    print("usage: gecko <command> [options]\n")
    print("commands:")
    print(
        "  serve <spec>       comprehend an OpenAPI spec and serve it to agents (MCP)"
    )
    print("  test  <spec>       generate + run first-call-correctness checks")
    print(
        "  from-docs <src>    recover a draft OpenAPI from a doc page, then comprehend"
    )
    print("  auth set|rm|list   hold your provider key in the OS keychain (BYOK)")
    print("  doctor [api]       diagnose the local MCP setup + print the add command")
    print("\nBare `gecko <spec>` is shorthand for `gecko serve <spec>`.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = _default_to_serve(argv)
    if cmd == "serve":
        return serve.main(rest)
    if cmd == "test":
        return _cmd_test(rest)
    if cmd == "from-docs":
        return _cmd_from_docs(rest)
    if cmd == "auth":
        return _cmd_auth(rest)
    if cmd == "doctor":
        return _cmd_doctor(rest)
    _print_help()
    return 0


def _run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _run()
