"""``gecko`` CLI — an argparse subcommand dispatcher. Thin by design.

Each verb is a thin wrapper over the package (all real logic lives in the
engine modules):

  * ``gecko add <api>``         one-command onboard: comprehend + wire into your agent
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
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path

from . import credentials, docs_reader, onboard, serve, testgen
from .access import public_session
from .client import AgentApiClient
from .netguard import UnsafeUrlError, validate_public_url

_SUBCOMMANDS = ("add", "serve", "test", "from-docs", "auth", "rm", "list", "doctor")
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


def _cmd_add(argv: list[str]) -> int:
    """`gecko add <api>` — comprehend an API and wire it into Claude Code (stdio).

    Thin transport: parse args, build the real ``AddDeps`` (network fetch,
    comprehend via the unmodified engine, hidden-prompt keychain store, real
    `claude mcp add` runner), and hand off to ``onboard.add`` for the logic.
    """
    p = argparse.ArgumentParser(
        prog="gecko add",
        description="Comprehend an API and wire it into your agent (stdio, key in keychain).",
    )
    p.add_argument("api", help="OpenAPI URL, docs URL, or local path.")
    p.add_argument(
        "--name", default=None, help="Surface name (default: derived from the ref)."
    )
    args = p.parse_args(argv)

    def _comprehend(spec: dict) -> int:
        return len(AgentApiClient(spec, session=public_session()).list_tools())

    def _store(name: str, secret: str) -> bool:
        ref = credentials.CredentialRef(api=name)
        backend = credentials.KeyringBackend()
        if not backend.available():
            # Mirror `_auth_set`'s remediation — never crash the onboard flow, and
            # never write plaintext anywhere. The surface still works (no-auth calls
            # or the key added later via the env fallback). Report failure so the
            # caller never claims the key was sealed.
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
            return False
        backend.store(ref, secret)
        return True

    deps = onboard.AddDeps(
        fetch=onboard._default_fetch,
        comprehend=_comprehend,
        prompt=lambda q: getpass.getpass(q),
        store=_store,
        run=onboard._default_run,
        home=Path.home(),
        resolver=None,  # real DNS in production; tests inject a fake resolver
    )
    return onboard.add(args.api, name=args.name, deps=deps)


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


def _cmd_rm(argv: list[str]) -> int:
    """`gecko rm <surface>` — deregister and delete a cached surface."""
    p = argparse.ArgumentParser(
        prog="gecko rm",
        description="Remove a cached surface from ~/.gecko/surfaces/ and deregister from Claude.",
    )
    p.add_argument("name", help="Surface name (as shown in `gecko list`).")
    args = p.parse_args(argv)
    return onboard.remove(args.name, run=onboard._default_run, home=Path.home())


def _cmd_list(argv: list[str]) -> int:
    """`gecko list` — list cached onboarded surfaces."""
    p = argparse.ArgumentParser(
        prog="gecko list",
        description="List all cached onboarded surfaces.",
    )
    p.parse_args(argv)
    surfaces = onboard.list_surfaces(home=Path.home())
    if not surfaces:
        print("No surfaces onboarded yet. Add one:  gecko add <api>")
        return 0
    for name in surfaces:
        print(f"  {name}")
    return 0


def _cmd_doctor(argv: list[str]) -> int:
    """`gecko doctor` — diagnose your setup and print the next step."""
    p = argparse.ArgumentParser(
        prog="gecko doctor",
        description="Check your setup and print the exact next step.",
    )
    p.parse_args(argv)

    print("Gecko doctor — check your setup\n" + "=" * 56)

    # 1. Gecko version
    try:
        version = importlib.metadata.version("gecko-surf")
        print(f"  ✓ gecko          {version}")
    except Exception:
        print("  ✗ gecko          unknown")

    # 2. Engine (AgentApiClient import)
    try:
        _ = AgentApiClient
        print("  ✓ engine         ok")
    except Exception as exc:
        print(f"  ✗ engine         {str(exc)}")

    # 3. OS keychain
    try:
        backend = credentials.KeyringBackend()
        if backend.available():
            print("  ✓ keychain       available")
        else:
            print("  ✗ keychain       not available (keys fall back to env vars)")
    except Exception:
        print("  ✗ keychain       not available")

    # 4. Claude Code CLI
    if shutil.which("claude"):
        print("  ✓ Claude Code CLI found")
    else:
        print(
            "  ✗ Claude Code CLI not found (install it or use `gecko serve … --stdio` manually)"
        )

    # 5. Onboarded surfaces
    try:
        surfaces = onboard.list_surfaces(home=Path.home())
        if surfaces:
            count = len(surfaces)
            names = ", ".join(surfaces)
            print(f"  ✓ surfaces       {count} onboarded ({names})")
        else:
            print("  ✗ surfaces       none — onboard one with `gecko add <api>`")
    except Exception:
        print("  ✗ surfaces       could not list")

    print("\n→ Next: onboard an API with `gecko add <api>` or `gecko add <url>`")
    return 0


_BLUE = "\x1b[38;2;20;110;245m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

_WORDMARK = r"""
  ▄▄ ▄▄▄ ▄▄  ▄  ▄  ▄▄▄
 ▐▌ ▐▌   ▐▌ ▟▙ ▐▌ ▐▌ ▐▌   G E C K O
 ▐▌▟▌▐▛▀ ▐▌ ▜▛ ▐▌ ▐▌ ▐▌
  ▀▀ ▀▀▀ ▀▀▀▘  ▀  ▀▀▀
""".rstrip("\n")


def _banner() -> str:
    """Return a GECKO ASCII wordmark, colored if TTY, plain otherwise."""
    color = sys.stdout.isatty()
    mark = (
        f"{_BLUE}{_WORDMARK}{_RESET}"
        if color
        else _WORDMARK.replace("G E C K O", "GECKO")
    )
    return mark


def _print_help() -> None:
    print(_banner())
    print("  make any API agent-usable — first call correct\n")
    print(f"{_BOLD}Onboard:{_RESET}" if sys.stdout.isatty() else "Onboard:")
    print("  add <api>          comprehend any API + wire it into your agent (stdio)")
    print("  rm <name>          remove an onboarded surface")
    print("  list               list onboarded surfaces")
    print("\nKeys:")
    print("  auth set|rm|list   hold your provider key in the OS keychain (BYOK)")
    print("\nDiagnose:")
    print("  doctor             check your setup, print the exact next step")
    print("\nAdvanced:")
    print("  serve <spec>       serve a comprehended spec to agents (MCP)")
    print("  from-docs <src>    recover a draft OpenAPI from a doc page")
    print("  test  <spec>       first-call-correctness checks")
    print("\nBare `gecko <spec>` is shorthand for `gecko serve <spec>`.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = _default_to_serve(argv)
    if cmd == "add":
        return _cmd_add(rest)
    if cmd == "serve":
        return serve.main(rest)
    if cmd == "test":
        return _cmd_test(rest)
    if cmd == "from-docs":
        return _cmd_from_docs(rest)
    if cmd == "auth":
        return _cmd_auth(rest)
    if cmd == "rm":
        return _cmd_rm(rest)
    if cmd == "list":
        return _cmd_list(rest)
    if cmd == "doctor":
        return _cmd_doctor(rest)
    _print_help()
    return 0


def _run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _run()
