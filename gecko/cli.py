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
import json
import sys

from . import docs_reader, serve, testgen
from .access import public_session
from .client import AgentApiClient
from .netguard import UnsafeUrlError, validate_public_url

_SUBCOMMANDS = ("serve", "test", "from-docs")
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
    _print_help()
    return 0


def _run() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    _run()
