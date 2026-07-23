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
from typing import Any

from . import (
    __version__,
    credentials,
    docs_reader,
    hosted_login,
    keyauth,
    login,
    onboard,
    serve,
    testgen,
)
from .access import public_session, stub_session
from .client import AgentApiClient
from .modes import coerce_mode
from .netguard import UnsafeUrlError, validate_public_url

_SUBCOMMANDS = (
    "add",
    "login",
    "connect",
    "keys",
    "serve",
    "test",
    "inspect",
    "from-docs",
    "auth",
    "graph",
    "rm",
    "list",
    "doctor",
    # Bundled ready-to-run example surfaces — also exposed as their own console
    # scripts, but registered here so the single `gecko` binary (and thus
    # `npx @geckovision/gecko <name>`) can run them with no local spec file.
    "jupiter-mcp",
    "colosseum-mcp",
    "txline-mcp",
)
# Below this many recovered ops we hint that agent-browser renders JS nav better.
_FEW_OPS = 2


def _default_to_serve(argv: list[str]) -> tuple[str, list[str]]:
    """Split argv into (command, rest), defaulting the legacy bare form to ``serve``.

    ``gecko <spec>`` (no subcommand) must behave exactly like ``gecko serve <spec>``,
    so anything that isn't a known subcommand token or a bare help/version flag is
    treated as the first positional of ``serve``. ``--version`` is intercepted HERE —
    before subcommand dispatch — so it never falls into the serve parser.
    """
    if not argv:
        return "help", []
    head = argv[0]
    if head in _SUBCOMMANDS:
        return head, argv[1:]
    if head in ("-h", "--help"):
        return "help", []
    if head == "--version":
        return "version", []
    return "serve", argv


def _print_key_clarity(spec: str) -> None:
    """Make the key situation explicit after a recorded run: everything was just tested
    ``$0`` with NO key, and this says which ops would additionally need one for LIVE data.
    Best-effort — clarity must never fail the command (a stub session unlocks the gated
    tools so they're countable offline)."""
    try:
        tools = AgentApiClient(spec, session=stub_session()).list_tools()
    except Exception:  # noqa: BLE001 — clarity is a nicety, never break `gecko test`
        return
    total = len(tools)
    gated = sum(1 for t in tools if t.get("requires_auth"))
    print("\n  ✓ simulated $0 in recorded mode — no API key needed.")
    if gated == 0:
        print("    This API needs no key at all: recorded and live both work keyless.")
    else:
        print(
            f"    {gated} of {total} tool(s) also need a key for LIVE calls — seal one "
            "with `gecko auth set <api>` when you want real data."
        )


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


def _key_prompt(question: str) -> str:
    """Hidden key prompt that degrades gracefully when there's no TTY.

    ``gecko add`` often runs under an agent, in CI, or with piped stdin — contexts
    with no controlling terminal, where ``getpass`` raises and would crash onboarding
    with a raw traceback (the worst possible first impression). Off a TTY, return ""
    so ``onboard.add`` takes its documented "no key entered — add later with
    `gecko auth set`" path and still wires the surface (recorded/$0 needs no key). The
    secret is never echoed or logged.
    """
    if not sys.stdin.isatty():
        return ""
    try:
        return getpass.getpass(question)
    except (EOFError, OSError):  # no usable terminal (termios error / closed stdin)
        return ""


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
    p.add_argument(
        "api",
        help="An API domain, OpenAPI URL, docs URL, or local path — Gecko finds the "
        "spec (probes common paths, else recovers one from the docs).",
    )
    p.add_argument(
        "--name", default=None, help="Surface name (default: derived from the ref)."
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Pin the request host explicitly — the one-line path for an API whose "
        "OpenAPI is served elsewhere (e.g. Colosseum). Enables live auth injection.",
    )
    p.add_argument(
        "--mode",
        choices=("recorded", "live"),
        default="recorded",
        help="Mode the integrated surface serves in: recorded ($0, synthesized — default) "
        "or live (real upstream calls, using the sealed key).",
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
        try:
            backend.store(ref, secret)
        except (credentials.CredentialError, OSError) as exc:
            # A mid-write failure (locked/broken keychain) must never crash `gecko
            # add` or leak the secret — report failure so the caller reports it as
            # "not sealed" (never a false "✓ sealed") and let the env fallback work.
            print(f"Could not write to the OS keychain: {exc}", file=sys.stderr)
            print(
                f"Use the env fallback instead:\n  export "
                f"{credentials.env_var_name(ref)}=...",
                file=sys.stderr,
            )
            return False
        return True

    deps = onboard.AddDeps(
        fetch=onboard._default_fetch,
        comprehend=_comprehend,
        prompt=_key_prompt,
        store=_store,
        run=onboard._default_run,
        home=Path.home(),
        resolver=None,  # real DNS in production; tests inject a fake resolver
        # Default-on adoption ping (aggregate-only, GECKO_TELEMETRY=off to disable);
        # wired ONLY here so library/test use of onboard.add stays network-silent.
        ping_post=onboard._default_ping_post,
    )
    return onboard.add(
        args.api, name=args.name, base_url=args.base_url, mode=args.mode, deps=deps
    )


def _cmd_inspect(argv: list[str]) -> int:
    """`gecko inspect <api>` — score an API's agent-readiness (offline, $0).

    Runs the four dimensions (first-call-correct, hygiene, agent-friendliness, security)
    and prints a graded scorecard. `--min-grade` gates a CI deploy; any blocking finding
    also exits non-zero (TDD-for-APIs).
    """
    from . import inspect as inspect_mod

    p = argparse.ArgumentParser(
        prog="gecko inspect",
        description="Score an API's agent-readiness (offline, $0): first-call-correct, "
        "spec hygiene, agent-friendliness, security.",
    )
    p.add_argument(
        "api",
        help="An API domain, OpenAPI URL, docs URL, or local path — Gecko finds the spec.",
    )
    p.add_argument(
        "-o", "--out", default=None, help="Also write the report as JSON to this path."
    )
    p.add_argument(
        "--min-grade",
        default=None,
        choices=("A", "B", "C", "D"),
        help="Exit non-zero if the grade is below this (CI gate).",
    )
    args = p.parse_args(argv)
    if _reject_unsafe(args.api, "inspect"):
        return 2
    try:
        resolved = onboard.resolve_spec(args.api)
    except onboard.OnboardError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2

    report = inspect_mod.inspect(resolved.spec, api=onboard.safe_name(args.api))
    print(inspect_mod.render(report))
    if args.out:
        import dataclasses

        Path(args.out).write_text(
            json.dumps(dataclasses.asdict(report), indent=2), encoding="utf-8"
        )
        print(f"\n  → wrote {args.out}")

    grade_order = "FDCBA"
    below = args.min_grade is not None and grade_order.index(
        report.grade
    ) < grade_order.index(args.min_grade)
    if below:
        print(
            f"\n  ✗ grade {report.grade} is below --min-grade {args.min_grade}",
            file=sys.stderr,
        )
    return 1 if (below or inspect_mod.has_blocking(report)) else 0


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
        results = testgen.check(args.spec, mode=coerce_mode(args.mode))
    except (UnsafeUrlError, ValueError) as exc:
        print(f"Could not comprehend spec: {exc}", file=sys.stderr)
        return 2

    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.tool} · {r.kind} — {r.detail}")
    passed, total = testgen.summary(results)
    print(f"\n{passed}/{total} checks passed ({args.mode} mode)")

    if args.mode == "recorded":
        _print_key_clarity(args.spec)

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


def _cmd_graph(argv: list[str]) -> int:
    """`gecko graph confirm|declared|rm` — thin transport over ``gecko.hints``.

    The §12 confirm loop: a human upgrades a relationship to DECLARED (the top of
    the §13.2 trust ladder — the only basis a cross-API join may plan on, §13.6)
    with an audit trail. The store holds the RELATIONSHIP (name → entity, when,
    what it upgraded) per surface — never traffic, never payloads (§14 guardrail).
    """
    from . import hints

    p = argparse.ArgumentParser(
        prog="gecko graph",
        description="Confirm and inspect DECLARED entity mappings for a surface.",
    )
    sub = p.add_subparsers(dest="action")

    p_confirm = sub.add_parser(
        "confirm",
        help="Confirm a param/field ↔ entity mapping (upgrades joins to DECLARED).",
    )
    p_confirm.add_argument("surface", help="Surface name, e.g. txline.")
    p_confirm.add_argument("name", help="The param/field name, e.g. FixtureId.")
    p_confirm.add_argument("entity", help="The entity it identifies, e.g. fixture.")
    p_confirm.add_argument(
        "--basis",
        default="",
        help="What this confirmation upgrades (e.g. an INFERRED edge's basis) — "
        "recorded in the audit trail.",
    )

    p_list = sub.add_parser(
        "declared", help="List the confirmed vocabulary for a surface."
    )
    p_list.add_argument("surface")

    p_rm = sub.add_parser("rm", help="Remove a confirmed mapping (idempotent).")
    p_rm.add_argument("surface")
    p_rm.add_argument("name")

    args = p.parse_args(argv)
    if args.action == "confirm":
        try:
            record = hints.confirm_entity(
                args.surface, args.name, args.entity, prior_basis=args.basis
            )
        except ValueError as exc:
            print(f"graph: {exc}", file=sys.stderr)
            return 1
        print(
            f"Confirmed {args.surface}: {record['name']} → {record['entity']} "
            f"(DECLARED; used on the next serve/graph build)."
        )
        return 0
    if args.action == "declared":
        records = hints.list_confirmed(args.surface)
        if not records:
            print(f"No confirmed mappings for '{args.surface}'.")
            return 0
        for r in records:
            print(
                f"  {r.get('name')} → {r.get('entity')}   "
                f"confirmed {r.get('confirmed_at', '?')}"
                + (f"   (upgraded: {r['prior_basis']})" if r.get("prior_basis") else "")
            )
        return 0
    if args.action == "rm":
        removed = hints.remove_confirmed(args.surface, args.name)
        print(
            f"Removed {args.surface}:{args.name}."
            if removed
            else f"No confirmed mapping {args.surface}:{args.name} (nothing to remove)."
        )
        return 0
    p.print_help()
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
    p_test.add_argument(
        "--live",
        action="store_true",
        help="Actually CALL the API to confirm the credential authenticates (a "
        "resolvable value can still be expired/revoked). Reports the HTTP status.",
    )
    p_test.add_argument(
        "--spec",
        default=None,
        help="OpenAPI spec (URL or path) for the --live probe. Auto for bundled "
        "surfaces (e.g. txline).",
    )
    p_test.add_argument(
        "--base-url",
        default=None,
        help="Host for the --live probe (default: the spec's first server).",
    )
    p_test.add_argument(
        "--op",
        default=None,
        help="Operation to probe (default: first auth-gated GET with no required args).",
    )

    args = p.parse_args(argv)
    if args.action == "set":
        return _auth_set(args.api, args.account, args.scheme)
    if args.action == "rm":
        return _auth_rm(args.api, args.account)
    if args.action == "list":
        return _auth_list()
    if args.action == "test":
        return _auth_test(
            args.api,
            args.account,
            live=args.live,
            spec_src=args.spec,
            base_url=args.base_url,
            op=args.op,
        )
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


def _auth_test(
    api: str,
    account: str | None,
    *,
    live: bool = False,
    spec_src: str | None = None,
    base_url: str | None = None,
    op: str | None = None,
) -> int:
    """Resolve the credential and report ONLY which backend answered — never the
    value, its length, or a prefix. ``which_backend`` reads the value internally to
    confirm a non-empty hit but never returns or logs it.

    With ``live=True``, go one step further and prove the credential actually
    AUTHENTICATES — a resolvable value can still be expired/revoked, and only a real
    call reveals that (the exact trap a stale TxODDS session sprang: resolved ✓, 401)."""
    if not live:
        # Resolve-only: does the keychain return a value for THIS exact slot?
        ref = credentials.CredentialRef(api=api, account=account)
        resolver = credentials.default_resolver()
        try:
            who = credentials.which_backend(ref, resolver)
        except credentials.CredentialError as exc:
            # A configured command that failed: error carries name + exit code only.
            print(f"auth: {exc}", file=sys.stderr)
            return 1
        if who is None:
            print(credentials.no_credential_message(ref), file=sys.stderr)
            return 1
        print(f"resolved ✓ via {who}")
        return 0

    # --live: the probe is authoritative. It builds the WHOLE surface session via
    # keychain_session (which resolves per-scheme slots for a multi-token API like
    # TxLINE), so it must NOT be gated on the bare `api` slot — that slot is empty by
    # design when creds live under `api:<scheme>` accounts.
    from . import authcheck

    if spec_src:
        from .ingest import load_spec

        spec, base = load_spec(spec_src), base_url
    else:
        target = authcheck.bundled_probe_target(api)
        if target is None:
            print(
                "--live needs a spec: pass --spec <url|path> "
                "(auto only for bundled surfaces like txline).",
                file=sys.stderr,
            )
            return 2
        spec, default_base = target
        base = base_url or default_base

    result = authcheck.live_probe(spec, api, base_url=base, op=op)
    mark = "✓" if result.ok else "✗"
    probed = f"  (probed {result.op})" if result.op else ""
    print(
        f"live {mark} {result.detail}{probed}",
        file=sys.stdout if result.ok else sys.stderr,
    )
    return 0 if result.ok else 1


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

    # 3. OS keychain — a real write→read→delete round-trip, not just "backend present".
    #    `available()` is True even for a keychain that refuses every write (an unsigned
    #    frozen macOS binary → errSecInteractionNotAllowed -25244), so only the round-trip
    #    tells the truth about whether `gecko login`/`connect` can actually seal a key.
    try:
        works, detail = credentials.KeyringBackend().selftest()
        if works:
            print("  ✓ keychain       works (write→read→delete ok)")
        else:
            print(f"  ✗ keychain       {detail}")
            print(
                "                   → the OS keychain is present but unusable here. On "
                "macOS this is\n                     typically an UNSIGNED binary "
                "(the npx/uvx frozen build). Try the\n                     Python install "
                "(`pipx install gecko-surf`) whose keychain access is\n                     "
                "signed, or run `gecko` from a signed build."
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ keychain       probe failed: {type(exc).__name__}")

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


_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

# figlet 'standard' GECKO — the universal block-letter wordmark style.
_WORDMARK = r"""  ____ _____ ____ _  _____
 / ___| ____/ ___| |/ / _ \
| |  _|  _|| |   | ' / | | |
| |_| | |__| |___| . \ |_| |
 \____|_____\____|_|\_\___/"""

# Brand gradient — Gecko blue -> green (the `| lolcat` look, but on-brand and
# self-contained: no external tool, so it renders in the shipped binary too).
_GRAD_START = (20, 110, 245)
_GRAD_END = (53, 208, 138)


def _gradient(art: str) -> str:
    """Color each column of the wordmark along the brand blue->green ramp."""
    lines = art.split("\n")
    span = max(max((len(ln) for ln in lines), default=1) - 1, 1)
    out = []
    for line in lines:
        buf = []
        for i, ch in enumerate(line):
            t = i / span
            r = round(_GRAD_START[0] + (_GRAD_END[0] - _GRAD_START[0]) * t)
            g = round(_GRAD_START[1] + (_GRAD_END[1] - _GRAD_START[1]) * t)
            b = round(_GRAD_START[2] + (_GRAD_END[2] - _GRAD_START[2]) * t)
            buf.append(f"\x1b[38;2;{r};{g};{b}m{ch}")
        out.append("".join(buf) + _RESET)
    return "\n".join(out)


def _banner() -> str:
    """GECKO wordmark — brand gradient on a TTY, plain block letters otherwise."""
    return _gradient(_WORDMARK) if sys.stdout.isatty() else _WORDMARK


def _print_help() -> None:
    print(_banner())
    print("  make any API agent-usable — first call correct\n")
    print(f"{_BOLD}Onboard:{_RESET}" if sys.stdout.isatty() else "Onboard:")
    print("  add <api>          comprehend any API + wire it into your agent (stdio)")
    print("  rm <name>          remove an onboarded surface")
    print("  list               list onboarded surfaces")
    print("\nKeys:")
    print("  login              enroll a hosted identity (key sealed, never shown)")
    print("  connect <surface>  use a gated hosted surface — key from the keychain")
    print("  auth set|rm|list   hold your provider key in the OS keychain (BYOK)")
    print("  keys mint|enable|disable|list <account>  founder access to gated surfaces")
    print("  keys grant|revoke <account> --surface X  per-surface access control")
    print("\nDiagnose:")
    print("  doctor             check your setup, print the exact next step")
    print("  --version          print the gecko version")
    print("\nAdvanced:")
    print("  serve <spec>       serve a comprehended spec to agents (MCP)")
    print("  from-docs <src>    recover a draft OpenAPI from a doc page")
    print("  test  <spec>       first-call-correctness checks")
    print("\nBare `gecko <spec>` is shorthand for `gecko serve <spec>`.")


def _cmd_login(argv: list[str]) -> int:
    """`gecko login` — enroll a hosted identity (email → one-time code → sealed Gecko key).

    Zero-config: it talks ONLY to Gecko's server, which runs identity (Privy is a server-side
    detail) and returns a minted Gecko key that is sealed in the OS keychain. Users never touch
    Privy or a ``PRIVY_APP_ID``. Local `gecko add` (recorded, $0) never needs this — login gates
    only the HOSTED plane (attribution, rate-limit, hosted features).

    Thin transport: parse args, build the keychain-store seam, hand off to
    ``hosted_login.hosted_login``. No secret is read client-side."""
    p = argparse.ArgumentParser(
        prog="gecko login",
        description="Enroll a hosted Gecko identity via an email one-time code. "
        "Local `gecko add` (recorded, $0) never needs this.",
    )
    p.add_argument("--email", default=None, help="Your email (prompted if omitted).")
    p.add_argument(
        "--server",
        default=hosted_login.DEFAULT_LOGIN_SERVER,
        help=f"Gecko login server. Defaults to {hosted_login.DEFAULT_LOGIN_SERVER}.",
    )
    args = p.parse_args(argv)

    email = args.email or input("Email: ")

    def _store(ref: credentials.CredentialRef, secret: str) -> bool:
        # Mirror onboard's sealing: seal in the OS keychain, report success as a bool so
        # login never falsely claims "logged in" when no keychain is available.
        backend = credentials.KeyringBackend()
        if not backend.available():
            return False
        try:
            backend.store(ref, secret)
        except (credentials.CredentialError, OSError):
            return False
        return True

    try:
        return hosted_login.hosted_login(
            email,
            server_url=args.server,
            prompt=input,
            store=_store,
            home=credentials.config_home(),
        )
    except login.LoginError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2


def _cmd_connect(argv: list[str]) -> int:
    """`gecko connect <surface>` — serve a GATED hosted surface over stdio, with the
    Gecko key read from the OS keychain instead of an MCP client config.

    The point is that the client config holds a command, not a credential::

        {"mcpServers": {"gecko-birdeye":
            {"command": "gecko", "args": ["connect", "birdeye"]}}}

    stdout IS the JSON-RPC channel once the bridge is running, so EVERY diagnostic here
    goes to stderr — a stray print would corrupt the protocol stream.

    Thin transport: parse args, hand off to ``connect.connect``.
    """
    from . import connect as connect_mod

    p = argparse.ArgumentParser(
        prog="gecko connect",
        description="Connect to a gated hosted Gecko surface using the key sealed by "
        "`gecko login` (never pasted into a config file).",
    )
    p.add_argument("surface", help="Hosted surface/mount name, e.g. 'birdeye'.")
    p.add_argument(
        "--host",
        default=connect_mod.DEFAULT_HOST,
        help=f"Hosted plane. Defaults to {connect_mod.DEFAULT_HOST}.",
    )
    args = p.parse_args(argv)

    try:
        connect_mod.connect(args.surface, host=args.host)
    except connect_mod.ConnectError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130
    return 0


def _keys_allowlist() -> Any:
    """The allowlist store for `gecko keys`: the Gecko-key REGISTRY when configured (hosted
    plane, toggles the minted keys' ``enabled``), else the local :class:`FileAllowlist`.

    ``registry_from_env`` returns ``None`` unless ``MONGODB_URI`` is set, so a normal local run
    keeps using the file store with zero behavior change; a founder with the hosted DB wired
    toggles the registry record instead. Both satisfy the enable/disable/accounts contract.
    """
    from .keyregistry import RegistryAllowlist, registry_from_env

    registry = registry_from_env()
    if registry is not None:
        return RegistryAllowlist(registry)
    return keyauth.FileAllowlist()


def _cmd_keys_mint(account: str, label: str, surfaces: list[str] | None = None) -> int:
    """`gecko keys mint <account>` — mint ONE Gecko key for a developer, printed once.

    The direct founder path to authorize a developer on a gated (paid) hosted surface,
    independent of the hosted email-OTP login. Reuses the SAME primitives the login
    endpoint uses (``keyregistry.mint_key`` + ``hash_key`` + ``store_key``) — one key
    format, one storage path.

    Security: only ``sha256(key) -> {account_id, created, enabled, label}`` is stored, so
    the plaintext key exists solely in this one stdout line — it is never logged, never
    persisted, and can never be re-retrieved (mint a new one and disable the old).
    """
    # Module-attr access (not `from ... import registry_from_env`) so the wiring stays
    # one indirection the tests can swap for the in-memory fake — no Mongo in the suite.
    from . import keyregistry
    from .keyregistry import hash_key, mint_key

    registry = keyregistry.registry_from_env()
    if registry is None:
        print(
            "  ✗ no Gecko key registry configured — set MONGODB_URI to the hosted "
            "registry and re-run (the key must live where the server can read it).",
            file=sys.stderr,
        )
        return 2
    try:
        account = _require_nonblank_account(account)
    except keyauth.KeyAuthError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    granted = sorted({s.strip() for s in (surfaces or []) if s.strip()})
    try:
        for surface in granted:
            keyauth._require_surface(surface)
    except keyauth.KeyAuthError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    key = mint_key()
    registry.store_key(
        key_hash=hash_key(key), account_id=account, label=label, surfaces=granted
    )
    scope = ", ".join(granted) if granted else "NO surfaces yet"
    print(f"Minted a Gecko key for {account} (enabled; {scope}).")
    print("Shown ONCE — copy it now; it is never stored in plaintext or retrievable:\n")
    print(f"  {key}\n")
    print("The developer sends it on every request to a gated surface:")
    print("  Authorization: Bearer <key>")
    if not granted:
        print(
            f"\nIt opens nothing until you grant a surface:\n"
            f"  gecko keys grant {account} --surface birdeye"
        )
    print(f"Revoke with:  gecko keys disable {account}")
    return 0


def _require_nonblank_account(account: str) -> str:
    account = (account or "").strip()
    if not account:
        raise keyauth.KeyAuthError("account id must be a non-empty identifier")
    return account


def _cmd_keys(argv: list[str]) -> int:
    """`gecko keys mint|enable|disable|list <account>` — founder-only developer access.

    Layer 1 access control: register which developer account ids may reach the hosted,
    Gecko-key-gated (paid) surfaces (see ``keyauth``). Thin transport over the allowlist
    store; the hosted deploy swaps in the registry-backed store behind the same
    ``Allowlist`` seam.

    Security: the allowlist holds only NON-SECRET account ids (the login identity's
    subject), never a token; the registry holds only a key HASH. ``list`` prints account
    ids only, and ``mint`` prints its key exactly once — never to a log.
    """
    p = argparse.ArgumentParser(
        prog="gecko keys",
        description="Founder-only: mint/enable/disable a developer account on the "
        "Gecko-key-gated hosted surfaces. Stores account ids + key hashes only.",
    )
    sub = p.add_subparsers(dest="action")

    p_mint = sub.add_parser(
        "mint", help="Mint a Gecko key for a developer (printed exactly once)."
    )
    p_mint.add_argument("account", help="The developer's stable account id.")
    p_mint.add_argument(
        "--surface",
        action="append",
        default=None,
        metavar="NAME",
        help="Grant a gated surface (repeatable). Omit and the key opens nothing.",
    )
    p_mint.add_argument(
        "--label",
        default="founder-minted",
        help="A non-secret note stored with the key (who/what it is for).",
    )

    p_enable = sub.add_parser(
        "enable", help="Allow a developer account (by account id)."
    )
    p_enable.add_argument("account", help="The developer's stable account id.")

    p_disable = sub.add_parser(
        "disable", help="Revoke a developer account (idempotent)."
    )
    p_disable.add_argument("account", help="The developer's stable account id.")

    p_grant = sub.add_parser(
        "grant", help="Grant one gated surface to a developer account."
    )
    p_grant.add_argument("account", help="The developer's stable account id.")
    p_grant.add_argument("--surface", required=True, help="Mount name, e.g. birdeye.")

    p_revoke = sub.add_parser("revoke", help="Revoke one gated surface (idempotent).")
    p_revoke.add_argument("account", help="The developer's stable account id.")
    p_revoke.add_argument("--surface", required=True, help="Mount name, e.g. birdeye.")

    sub.add_parser(
        "list", help="List enabled account IDs + their grants (never a token)."
    )

    args = p.parse_args(argv)
    if args.action == "mint":
        # Minting needs the REGISTRY itself (the allowlist seam only toggles `enabled`).
        return _cmd_keys_mint(args.account, args.label, args.surface)
    store = _keys_allowlist()
    try:
        if args.action == "enable":
            added = store.enable(args.account)
            print(
                f"Enabled {args.account}."
                if added
                else f"{args.account} was already enabled."
            )
            return 0
        if args.action == "disable":
            removed = store.disable(args.account)
            print(
                f"Disabled {args.account}."
                if removed
                else f"{args.account} was not enabled (nothing to do)."
            )
            return 0
        if args.action == "grant":
            surface = keyauth._require_surface(args.surface)
            added = store.grant(args.account, surface)
            print(
                f"Granted {surface} to {args.account}."
                if added
                else f"{args.account} already had {surface}."
            )
            return 0
        if args.action == "revoke":
            surface = keyauth._require_surface(args.surface)
            removed = store.revoke(args.account, surface)
            print(
                f"Revoked {surface} from {args.account}."
                if removed
                else f"{args.account} did not have {surface} (nothing to do)."
            )
            return 0
        if args.action == "list":
            accounts = store.accounts()
            if not accounts:
                print("No accounts enabled. Enable one:  gecko keys enable <account>")
            else:
                for account in accounts:
                    held = store.grants_for(account)
                    scope = ", ".join(held) if held else "no surfaces granted"
                    print(f"  {account}  ({scope})")
            return 0
    except keyauth.KeyAuthError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        return 2
    p.print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd, rest = _default_to_serve(argv)
    if cmd == "version":
        # Same source of truth as doctor: the installed package version.
        print(f"gecko {__version__}")
        return 0
    if cmd == "add":
        return _cmd_add(rest)
    if cmd == "login":
        return _cmd_login(rest)
    if cmd == "connect":
        return _cmd_connect(rest)
    if cmd == "keys":
        return _cmd_keys(rest)
    if cmd == "serve":
        # Wire the real first-run ping transport ONLY here (mirrors _cmd_add): the
        # CLI is default-on; library/test calls of serve.main stay network-silent.
        return serve.main(rest, ping_post=onboard._default_ping_post)
    if cmd == "jupiter-mcp":
        from .examples import jupiter  # lazy: pulls serve deps only when invoked

        return jupiter.main(rest)
    if cmd == "colosseum-mcp":
        from .examples import colosseum  # lazy: pulls serve deps only when invoked

        return colosseum.main(rest)
    if cmd == "txline-mcp":
        from .examples import txline  # lazy: pulls serve deps only when invoked

        return txline.main(rest)
    if cmd == "test":
        return _cmd_test(rest)
    if cmd == "inspect":
        return _cmd_inspect(rest)
    if cmd == "from-docs":
        return _cmd_from_docs(rest)
    if cmd == "auth":
        return _cmd_auth(rest)
    if cmd == "graph":
        return _cmd_graph(rest)
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
