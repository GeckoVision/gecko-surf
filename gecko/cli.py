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
    events,
    hosted_login,
    keyauth,
    login,
    onboard,
    serve,
    telemetry,
    testgen,
)
from .access import public_session, stub_session
from .client import AgentApiClient
from .ingest import load_spec
from .modes import CallMode, coerce_mode
from .netguard import UnsafeUrlError, validate_public_url

_SUBCOMMANDS = (
    "add",
    "prove",
    "watch",
    "login",
    "connect",
    "keys",
    "serve",
    "test",
    "inspect",
    "report",
    "verify-docs",
    "from-docs",
    "scan-image",
    "scan-doc",
    "auth",
    "graph",
    "correlate",
    "export-arazzo",
    "workflows",
    "index",
    "metrics",
    "drift",
    "rm",
    "list",
    "doctor",
    # Bundled ready-to-run example surfaces — also exposed as their own console
    # scripts, but registered here so the single `gecko` binary (and thus
    # `npx @geckovision/gecko <name>`) can run them with no local spec file.
    "jupiter-mcp",
    "colosseum-mcp",
    "txline-mcp",
    # The Orquestra provider surface — `gecko orquestra --program <name>`, so
    # `npx @geckovision/gecko orquestra --program meteora` reaches it too. Needs the
    # binary built with the [solana] extra for PDA derivation (see release.yaml).
    "orquestra",
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


def _cmd_report(argv: list[str]) -> int:
    """`gecko report <spec>` — the Agent-Readiness Scorecard (offline, $0).

    Thin transport over :mod:`gecko.report`. Without `--since`, write the self-contained
    HTML scorecard (plus a sidecar JSON for later diffs). With `--since <prior.json>`,
    print the drift delta (score change + broken/resolved call-paths).
    """
    from . import inspect as inspect_mod
    from . import report as report_mod
    from .surface import Surface

    p = argparse.ArgumentParser(
        prog="gecko report",
        description="Build a self-contained agent-readiness scorecard (HTML) for an API.",
    )
    p.add_argument(
        "spec",
        help="An API domain, OpenAPI URL, docs URL, or local path — Gecko finds the spec.",
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="Write the HTML here (default: <api>.scorecard.html).",
    )
    p.add_argument(
        "--intent",
        action="append",
        default=None,
        help="A plain-English intent to script the Playground (repeatable).",
    )
    p.add_argument(
        "--since",
        default=None,
        help="A prior scorecard JSON — print the drift delta instead of writing HTML.",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="Check each op against reality and render the verify-verdict badges.",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="With --verify, opt into real upstream calls (default: recorded, $0).",
    )
    p.add_argument(
        "--peer",
        action="append",
        default=[],
        help="A peer spec to count CONFIRMED cross-provider joins against (repeatable) — "
        "renders the correlated scorecard.",
    )
    p.add_argument(
        "--peer-id",
        action="append",
        default=[],
        help="Surface id for the matching --peer (repeatable, positional).",
    )
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="NAME=ENTITY",
        help="Customer-CONFIRMED value-domain hint for the primary surface (the "
        "join-plan gate; repeatable).",
    )
    p.add_argument(
        "--peer-confirm",
        action="append",
        default=[],
        metavar="NAME=ENTITY",
        help="Customer-CONFIRMED value-domain hint applied to EVERY peer surface.",
    )
    args = p.parse_args(argv)
    if _reject_unsafe(args.spec, "report"):
        return 2
    for peer_spec in args.peer:
        if _reject_unsafe(peer_spec, "report"):
            return 2
    # Load once, directly — mirrors `gecko graph` (load_spec handles both a local
    # yaml/json path and an SSRF-validated URL). The engine does the work.
    try:
        spec = load_spec(args.spec)
    except (UnsafeUrlError, OSError, ValueError) as exc:
        print(f"  ✗ could not read spec at {args.spec}: {exc}", file=sys.stderr)
        return 2

    name = onboard.safe_name(args.spec)
    if args.since:
        old = json.loads(Path(args.since).read_text(encoding="utf-8"))
        new = inspect_mod.inspect(spec, api=name).to_dict()
        print(report_mod.render_diff(report_mod.report_diff(old, new)))
        return 0

    # Peer surfaces power the correlated (multi-API) scorecard — same load pattern as
    # `gecko metrics`. Building a Surface does no network I/O beyond the one spec read.
    peer_confirm = _parse_kv(args.peer_confirm)
    peers = [
        Surface.of(
            AgentApiClient(
                peer_spec,
                session=public_session(),
                surface_id=(args.peer_id[i] if i < len(args.peer_id) else f"peer{i}"),
                declared_hints=peer_confirm or None,
            )
        )
        for i, peer_spec in enumerate(args.peer)
    ]

    verify_mode: CallMode = "live" if args.live else "recorded"
    html = report_mod.build_scorecard(
        spec,
        intents=args.intent,
        verify=args.verify,
        verify_mode=verify_mode,
        peers=peers or None,
        confirmed=_parse_kv(args.confirm) or None,
    )
    out = Path(args.out) if args.out else Path(f"{name}.scorecard.html")
    out.write_text(html, encoding="utf-8")
    sidecar = out.with_suffix(".json")
    sidecar.write_text(
        json.dumps(inspect_mod.inspect(spec, api=name).to_dict(), indent=2),
        encoding="utf-8",
    )
    # Print the ABSOLUTE path so the leave-behind is findable regardless of CWD — Raff
    # hit "wrote <name>.scorecard.html" with no hint of where the file landed.
    print(f"  → wrote {out.resolve()} ({len(html)} bytes)")
    print(f"  → wrote {sidecar.resolve()}")

    # The graph report. The Scorecard GRADES the surface; this DESCRIBES it — the spine
    # most operations hang off, the producer→consumer hops, and the inputs nothing here
    # produces. Derived from the graph alone, so it costs nothing extra and cannot fail
    # the grading run: a report we could not build is skipped with a reason, never
    # rendered blank (a blank section reads as "no problems found").
    try:
        from . import surfacereport as graph_report_mod
        from .surface import Surface

        graph = Surface.of(AgentApiClient(spec, surface_id=name)).graph
        graph_report = graph_report_mod.build_report(graph, name)
        report_path = out.with_name(f"{out.stem}.graph-report.md")
        report_path.write_text(
            graph_report_mod.render_markdown(graph_report), encoding="utf-8"
        )
        print(f"  → wrote {report_path.resolve()}")
        print(f"    {graph_report_mod.summarize(graph_report)}")
    except Exception as exc:  # pragma: no cover - defensive, never fails the scorecard
        print(f"  → graph report skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


def _cmd_verify_docs(argv: list[str]) -> int:
    """`gecko verify-docs <spec> [--live]` — check ops against reality (VAS-2).

    Thin transport over :mod:`gecko.verify`: build the surface, verify every op, print the
    control-plane JSON report ({op_id: {status, basis, provenance}} + counts). Default
    recorded (offline, $0 — every op honestly UNVERIFIED); ``--live`` opts into real calls
    (2xx VERIFIES, a 404 on a doc-claimed endpoint REFUTES).

    The DECLARED value-domain vocabulary is applied BY DEFAULT: the surface's stored
    customer confirmations (``gecko graph confirm``) are loaded here and, with the spec's
    own ``x-gecko`` hints (read inside the client), let example synthesis fill a REAL
    argument for a known value domain instead of a placeholder that would 404 a healthy
    endpoint. ``--confirm NAME=ENTITY`` declares one inline for this run.
    """
    from . import hints as hints_mod, verify as verify_mod

    p = argparse.ArgumentParser(
        prog="gecko verify-docs",
        description="Verify a surface's operations against the real API and report verdicts.",
    )
    p.add_argument(
        "spec",
        help="An API domain, OpenAPI URL, docs URL, or local path — Gecko finds the spec.",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Opt into real upstream calls (default: recorded, offline, $0).",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help="Pin the live target host (needed when a local spec's server can't be trusted).",
    )
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="NAME=ENTITY",
        help="Customer-CONFIRMED value-domain hint for a param, so verify can fill a REAL "
        "argument instead of a placeholder (repeatable).",
    )
    args = p.parse_args(argv)
    if _reject_unsafe(args.spec, "verify-docs"):
        return 2
    try:
        spec = load_spec(args.spec)
    except (UnsafeUrlError, OSError, ValueError) as exc:
        print(f"  ✗ could not read spec at {args.spec}: {exc}", file=sys.stderr)
        return 2

    mode: CallMode = "live" if args.live else "recorded"
    # A LIVE verify against a keyless/public API must use the no-auth adapter: stub_session
    # reports has_auth=True, which makes keyless reads on an un-pinned local spec degrade
    # live -> recorded (they can never VERIFY). public_session injects nothing, so keyless
    # reads fire and auth-gated ops it cannot satisfy are honestly hidden. Recorded never
    # hits the wire, so the stub (byte-identical legacy behaviour) is fine there.
    session = public_session() if mode == "live" else stub_session()
    # Stored confirmations first, this run's --confirm on top (an explicit flag wins), and
    # both are passed at CONSTRUCTION — the canonical example is stamped during tool build,
    # so a post-hoc merge would never reach example synthesis.
    surface = onboard.safe_name(args.spec)
    try:
        declared = hints_mod.load_confirmed(surface)
    except Exception:  # noqa: BLE001 - a corrupt local hint file must not stop verify
        declared = {}
    declared.update(_parse_kv(args.confirm))
    client = AgentApiClient(
        spec,
        base_url=args.base_url,
        session=session,
        surface_id=surface,
        declared_hints=declared or None,
    )
    # No ``corpus_path`` here, so a verify run persists nothing today. If one is ever
    # wired in, it stays safe: ``verify_docs`` redirects the client's capture to the
    # segregated ``corpus.selfcheck_sibling`` for the whole run, because these calls use
    # arguments Gecko invented and must never enter the observed agent denominator.
    report = verify_mod.verify_docs(client, mode=mode)
    print(json.dumps(report, indent=2))
    return 0


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
        help="recorded ($0, synthesized) or live (real upstream calls + sealed auth).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="Shorthand for --mode live: run the suite against the real API "
        "(sealed credentials). Default stays recorded ($0).",
    )
    p.add_argument(
        "-o",
        "--out",
        default=None,
        help="Also write a standalone pytest module here (commit it to CI).",
    )
    args = p.parse_args(argv)

    # --live is the ergonomic shorthand; it wins over the recorded default. Explicit
    # --mode live is equivalent. Keeping both means `serve --mode` users and agents
    # reaching for the obvious `--live` flag both land on the live suite.
    mode: CallMode = "live" if (args.live or args.mode == "live") else "recorded"

    if _reject_unsafe(args.spec, "ingest"):
        return 2

    # Live mode runs the suite against the REAL upstream with sealed credentials. The
    # session is resolved off the SPEC's own declared security schemes via the same
    # keychain seam `serve --auth-keychain` uses (never a hardcoded header/scheme). A
    # spec with no header-shaped scheme degrades to no-auth + a printed warning, exactly
    # like serve — the run never crashes. Recorded needs no session ($0, synthesized).
    session = None
    if mode == "live":
        from .access import keychain_session

        try:
            spec_dict = load_spec(args.spec)
        except (UnsafeUrlError, OSError, ValueError) as exc:
            print(f"Could not comprehend spec: {exc}", file=sys.stderr)
            return 2
        session, warning = keychain_session(spec_dict, onboard.safe_name(args.spec))
        if warning:
            print(f"  ⚠ {warning}", file=sys.stderr)

    try:
        results = testgen.check(args.spec, mode=coerce_mode(mode), session=session)
    except (UnsafeUrlError, ValueError) as exc:
        print(f"Could not comprehend spec: {exc}", file=sys.stderr)
        return 2

    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.tool} · {r.kind} — {r.detail}")
    passed, total = testgen.summary(results)
    print(f"\n{passed}/{total} checks passed ({mode} mode)")

    if mode == "recorded":
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


#: Skill Guard exit-code convention (shared by scan-image / scan-doc): a POISON
#: verdict exits non-zero so the command is CI/pipe-usable; CLEAN and REVIEW both
#: exit 0 (REVIEW is a soft "a human should look", not a hard block).
_SCAN_POISON_EXIT = 2

#: R9: a scan that COULD NOT RUN must not render as a scan that PASSED. When a channel
#: an attack class actually uses was unreadable, there is no verdict to report on it —
#: so the command does not report one. Distinct from POISON on purpose: 2 means "the
#: scanner ran and found an attack", 3 means "the scanner could not run". Both are
#: non-zero because `gecko scan-image x.png && deploy` must not pass on an unscanned
#: channel; REVIEW stays 0 because REVIEW is a verdict, not a failure to produce one.
_SCAN_INCOMPLETE_EXIT = 3

#: What each unreadable channel means and how an operator restores it. Channel NAMES
#: only (control plane) — never a probed path or binary location.
_CHANNEL_REMEDIATION = {
    "ocr": (
        "rendered pixels (the channel an image-borne injection is rendered INTO) — "
        "install the [ocr] extra (pip install 'gecko-surf[ocr]'; self-contained on "
        "Linux/macOS, needs a system tesseract on Windows)"
    ),
    "deep-metadata": (
        "EXIF / XMP / IPTC / ICC — install the [imagescan] extra "
        "(stdlib PNG tEXt/iTXt/zTXt + JPEG COM/APPn were still scanned)"
    ),
}


def _print_scan_verdict(
    tier: str,
    basis: tuple[str, ...],
    *,
    channels_scanned: int | None = None,
    channels_unavailable: tuple[str, ...] = (),
) -> None:
    """Print one Skill Guard verdict, demo-legibly (Video 1 shows this).

    Presentation only — the tiering, the basis and the coverage are computed in the
    package. The header names the tier; the basis lines name WHY (channel + rule, never
    the payload); the coverage block names what could NOT be read.

    The one presentation RULE this enforces (R9): the unqualified "CLEAN — no injection
    or exfil signal found" header is printed only when coverage was complete. With a
    channel unread, "no signal found" would be an over-claim — nothing was looked for
    there — so the header states the gap instead. POISON still outranks it: a found
    attack is actionable now, and the coverage block prints underneath it anyway.
    """
    headers = {
        "poison": "POISON — quarantined (fail-closed: recorded-only until a human clears)",
        "review": "REVIEW — flagged, a human should look",
        "clean": "CLEAN — no injection or exfil signal found",
    }
    if channels_unavailable and tier != "poison":
        print("INCOMPLETE — a channel this attack class uses could not be read")
    else:
        print(headers.get(tier, tier.upper()))
    if channels_scanned is not None:
        # A zero count under a pass is ambiguous to a reader in exactly the way this
        # whole surface exists to avoid: "nothing was read" and "everything was read
        # and carried no text" are different claims. When coverage is complete the
        # count can only mean the second, so say so rather than leave the arithmetic
        # to be read as a fail-open.
        if channels_scanned == 0 and not channels_unavailable:
            print(
                "  channels scanned: 0 "
                "(every channel was readable; none carried text to scan)"
            )
        else:
            print(f"  channels scanned: {channels_scanned}")
    if basis:
        print("  basis:")
        for reason in basis:
            print(f"    - {reason}")
    else:
        print("  basis: (none)")
    if channels_unavailable:
        print("  could not scan:")
        for channel in channels_unavailable:
            detail = _CHANNEL_REMEDIATION.get(channel, "")
            print(f"    - {channel}: {detail}" if detail else f"    - {channel}")
        print("  no verdict is claimed for the channel(s) above.")


def _cmd_scan_image(argv: list[str]) -> int:
    """`gecko scan-image <path>` — scan one image for an image-borne injection.

    Thin transport: read the file bytes, hand them to ``imagescan.scan_image``
    (L2 stdlib metadata/trailing-bytes always; L3 OCR + Pillow deep metadata only
    when those extras are present), print the verdict. Non-zero exit on POISON, and
    (R9) on an INCOMPLETE scan — one where a channel could not be read at all.

    ``--allow-missing-channels`` is informed consent, not a mute: it buys back the zero
    exit for an operator who has accepted the residual, while the coverage block is still
    printed. Without it, the [ocr] extra would be de-facto mandatory for a passing run;
    with it, what is mandatory is only the acknowledgement.
    """
    p = argparse.ArgumentParser(
        prog="gecko scan-image",
        description="Scan an image for an image-borne injection (Skill Guard). L2 "
        "(stdlib metadata + trailing bytes) always runs; the [ocr] and [imagescan] "
        "extras add rendered-pixel OCR + deep metadata when installed. Exits "
        f"{_SCAN_POISON_EXIT} on POISON, {_SCAN_INCOMPLETE_EXIT} when a channel could "
        "not be read at all, 0 on a complete CLEAN/REVIEW.",
    )
    p.add_argument("path", help="Path to a PNG/JPEG image file.")
    p.add_argument(
        "--allow-missing-channels",
        action="store_true",
        help="Exit 0 even when a channel could not be scanned. The coverage caveat is "
        "still printed — this suppresses the exit code, never the disclosure.",
    )
    args = p.parse_args(argv)

    from . import imagescan

    try:
        data = Path(args.path).read_bytes()
    except OSError as exc:
        # NOT the POISON exit: a file we could not open was never evaluated, and exit 2
        # would claim it was and failed. Still non-zero, so `scan && deploy` blocks.
        print(f"Could not read image: {exc}", file=sys.stderr)
        return _SCAN_INCOMPLETE_EXIT

    verdict = imagescan.scan_image(data)
    print(f"Gecko scan-image — {args.path}\n" + "=" * 56)
    _print_scan_verdict(
        verdict.tier,
        verdict.basis,
        channels_scanned=verdict.channels_scanned,
        channels_unavailable=verdict.channels_unavailable,
    )
    if verdict.tier == "poison":
        return _SCAN_POISON_EXIT
    if verdict.channels_unavailable and not args.allow_missing_channels:
        return _SCAN_INCOMPLETE_EXIT
    return 0


def _cmd_scan_doc(argv: list[str]) -> int:
    """`gecko scan-doc <path>` — scan one untrusted doc/convention page.

    Thin transport: read the text file, hand it to ``docs_reader.scan.scan_doc_page``
    (L1 convention-text tells + any inline ``data:`` image scanned as L2/L3), print
    the verdict. Non-zero exit on POISON.
    """
    p = argparse.ArgumentParser(
        prog="gecko scan-doc",
        description="Scan an untrusted convention/doc page for a follow-rendered + "
        "exfil delivery (Skill Guard L1) and any inline data: image (L2/L3). Exits "
        f"{_SCAN_POISON_EXIT} on POISON, {_SCAN_INCOMPLETE_EXIT} when an embedded "
        "image's channel could not be read at all, 0 on a complete CLEAN/REVIEW.",
    )
    p.add_argument("path", help="Path to a text/markdown doc file.")
    p.add_argument(
        "--allow-missing-channels",
        action="store_true",
        help="Exit 0 even when a channel could not be scanned. The coverage caveat is "
        "still printed — this suppresses the exit code, never the disclosure.",
    )
    args = p.parse_args(argv)

    from .docs_reader.scan import scan_doc_page

    try:
        text = Path(args.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Same rule as scan-image: unopened is "not evaluated" (3), not "failed" (2).
        print(f"Could not read doc: {exc}", file=sys.stderr)
        return _SCAN_INCOMPLETE_EXIT

    verdict = scan_doc_page(text)
    tier = (
        "poison"
        if verdict.poison_basis
        else ("review" if verdict.review_basis else "clean")
    )
    basis = verdict.poison_basis or verdict.review_basis
    print(f"Gecko scan-doc — {args.path}\n" + "=" * 56)
    _print_scan_verdict(tier, basis, channels_unavailable=verdict.unavailable_channels)
    # Same rule as scan-image, and for the same reason: L1 catching the FLAGSHIP delivery
    # file is not a guarantee it catches every delivery. An attacker who omits the L1
    # tells and carries the whole payload in the embedded image defeats L1 exactly as it
    # defeats L2 — so an unread image channel on this path is the same blind spot, and
    # gets the same non-zero exit.
    if tier == "poison":
        return _SCAN_POISON_EXIT
    if verdict.unavailable_channels and not args.allow_missing_channels:
        return _SCAN_INCOMPLETE_EXIT
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

    p_svg = sub.add_parser(
        "svg", help="Render the surface as an SVG call graph (graphviz for APIs)."
    )
    p_svg.add_argument("spec", help="An OpenAPI URL, path, or docs URL.")
    p_svg.add_argument(
        "-o", "--out", default=None, help="Write to a file (default: stdout)."
    )

    p_json = sub.add_parser(
        "json",
        help="Print the surface's call graph as structured JSON (nodes + edges).",
    )
    p_json.add_argument("spec", help="An OpenAPI URL, path, or docs URL.")
    p_json.add_argument(
        "-o", "--out", default=None, help="Write to a file (default: stdout)."
    )

    args = p.parse_args(argv)
    if args.action in ("svg", "json"):
        from .access import public_session
        from .surface import Surface

        surface = Surface.from_spec(args.spec, session=public_session())
        if args.action == "svg":
            text = surface.render_svg()
        else:
            text = json.dumps(surface.graph_data(), indent=2)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"Wrote {args.out} ({len(text)} bytes).")
        else:
            print(text)
        return 0
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


def _cmd_correlate(argv: list[str]) -> int:
    """`gecko correlate <specA> <specB>` — the cross-API correlation report.

    Thin transport, like `gecko graph json`: build two Surfaces, call
    ``Surface.correlate``, print the provenance-carrying result as JSON. Cross-API
    links are DECLARED-only for plan-eligibility (§13.6); a bare name/signature
    match across the boundary is a quarantined candidate a human confirms. This is a
    report — non-zero exit is not used.
    """
    from .access import public_session
    from .surface import Surface

    p = argparse.ArgumentParser(
        prog="gecko correlate",
        description="Report which of A's outputs correlate to B's inputs, with provenance.",
    )
    p.add_argument("spec_a", help="An OpenAPI URL, path, or docs URL (surface A).")
    p.add_argument("spec_b", help="An OpenAPI URL, path, or docs URL (surface B).")
    p.add_argument("--id-a", default="A", help="Surface id label for A (default: A).")
    p.add_argument("--id-b", default="B", help="Surface id label for B (default: B).")
    args = p.parse_args(argv)

    a = Surface.from_spec(args.spec_a, session=public_session(), surface_id=args.id_a)
    b = Surface.from_spec(args.spec_b, session=public_session(), surface_id=args.id_b)
    print(json.dumps(a.correlate(b).to_dict(), indent=2))
    return 0


def _cmd_export_arazzo(argv: list[str]) -> int:
    """`gecko export-arazzo <spec> [<spec> ...] --op <operationId>` — the derived plan
    as a portable Arazzo 1.0 document.

    Thin transport, like `gecko correlate`: build a Surface per spec, compose the
    safety-gated chain with the shipped ``compose_safe_chain``, serialize with the pure
    ``gecko.arazzo.to_arazzo``, print JSON. All logic lives in the package.

    Exit 0 when the document is executable; **3 when it is a refusal** — a quarantined
    hop, an unconfirmed cross-API join, or no confident plan. A refused document is still
    printed (the refusal is the artifact), but it carries no workflow and deliberately
    does not validate as Arazzo, so a runtime cannot execute what Gecko refused.
    """
    from .access import public_session
    from .arazzo import is_executable, to_arazzo
    from .hints import load_confirmed
    from .safechain import compose_safe_chain
    from .surface import Surface

    p = argparse.ArgumentParser(
        prog="gecko export-arazzo",
        description="Export the derived plan as an Arazzo 1.0 document (no values).",
    )
    p.add_argument(
        "specs", nargs="+", help="One or more OpenAPI URLs, paths, or docs URLs."
    )
    p.add_argument("--op", required=True, help="The target operationId to plan for.")
    p.add_argument(
        "--id",
        action="append",
        default=[],
        help="Surface id per spec, in order (default: s0, s1, …).",
    )
    p.add_argument(
        "--target",
        default=None,
        help="Surface id owning --op (default: the LAST spec's surface id).",
    )
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        help="NAME=ENTITY customer confirmation, applied to every surface. Merged over "
        "the persisted `gecko graph confirm` store.",
    )
    p.add_argument(
        "--satisfied",
        action="append",
        default=[],
        help="An input the caller already holds (repeatable).",
    )
    p.add_argument("--title", default="Gecko derived plan")
    p.add_argument(
        "-o", "--out", default=None, help="Write to a file (default: stdout)."
    )
    args = p.parse_args(argv)

    extra = _parse_kv(args.confirm)
    ids = [args.id[i] if i < len(args.id) else f"s{i}" for i in range(len(args.specs))]
    surfaces = {
        sid: Surface.from_spec(
            spec,
            session=public_session(),
            surface_id=sid,
            declared_hints={**load_confirmed(sid), **extra} or None,
        )
        for sid, spec in zip(ids, args.specs)
    }
    target = args.target or ids[-1]
    if target not in surfaces:
        print(f"export-arazzo: unknown target surface '{target}'.", file=sys.stderr)
        return 2

    chain = compose_safe_chain(surfaces, target, args.op, args.satisfied)
    doc = to_arazzo(
        chain,
        graphs=tuple(s.graph for s in surfaces.values()),
        sources=dict(zip(ids, args.specs)),
        title=args.title,
        refusal_reason=(
            f"no confident plan for '{args.op}' on '{target}' — a cross-API join needs "
            "both sides customer-confirmed (`gecko graph confirm`)"
        ),
    )
    text = json.dumps(doc, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Wrote {args.out} ({len(text)} bytes).")
    else:
        print(text)
    return 0 if is_executable(doc) else 3


def _print_withheld(withheld: list[Any]) -> None:
    """Name the state-changing targets we refused to derive. Excluded is not hidden — a
    provider should see which of their write operations sit one hop from a context-free
    call, which is the interesting half, and why we will not choose one for them."""
    if not withheld:
        return
    print(
        f"\n  withheld {len(withheld)} state-changing target(s) — a derived workflow "
        "must not choose a side effect on your behalf:"
    )
    for w in withheld:
        print(f"    {w.method.upper():6} {w.target}  (reachable from {w.producer})")
    print("    Name one explicitly with `gecko export-arazzo --op <id>` to accept it.")


def _cmd_workflows(argv: list[str]) -> int:
    """`gecko workflows <spec>` — the workflows an agent will want, derived and ranked.

    `export-arazzo` takes `--op`, which means the provider must already know which
    workflow they want. This removes that: derive the candidates from the graph, rank
    them, and emit each as a validated Arazzo document plus a markdown index.

    Thin transport — the ranking lives in :mod:`gecko.workflows`, the planning in
    ``compose_safe_chain``, the serialization in ``to_arazzo``. Nothing new is invented
    here; the only new idea is CHOOSING the targets.
    """
    from .access import public_session
    from .arazzo import is_executable, to_arazzo
    from .hints import load_confirmed
    from .safechain import compose_safe_chain
    from .surface import Surface
    from .workflows import (
        derive_candidates,
        derive_write_targets,
        describe,
        render_index,
    )

    p = argparse.ArgumentParser(
        prog="gecko workflows",
        description="Derive and rank the agent workflows an API surface can offer, and "
        "write each as an Arazzo 1.0 document. Offline, $0, no model call.",
    )
    p.add_argument("spec", help="An OpenAPI URL, path, or docs URL.")
    p.add_argument(
        "--id", default=None, help="Surface id (default: derived from spec)."
    )
    p.add_argument("--limit", type=int, default=7, help="How many to emit (default 7).")
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        help="NAME=ENTITY customer confirmation. Merged over the persisted store.",
    )
    p.add_argument(
        "-o",
        "--out",
        default=".",
        help="Directory for the emitted documents (default: the working directory).",
    )
    args = p.parse_args(argv)

    sid = args.id or onboard.safe_name(args.spec)
    surface = Surface.from_spec(
        args.spec,
        session=public_session(),
        surface_id=sid,
        declared_hints={**load_confirmed(sid), **_parse_kv(args.confirm)} or None,
    )
    candidates = derive_candidates(surface.graph, limit=args.limit)
    withheld = derive_write_targets(surface.graph)
    if not candidates:
        # A surface with no chainable hop is a real answer, not an error: every call
        # stands alone and an agent needs no plan. Saying so beats writing zero files
        # and letting the operator guess whether it ran.
        print(
            f"{sid}: no derivable workflow — no operation here produces a value another "
            "one accepts that the planner will chain on.",
        )
        _print_withheld(withheld)
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    refused = 0
    for candidate in candidates:
        chain = compose_safe_chain({sid: surface}, sid, candidate.target, [])
        doc = to_arazzo(
            chain,
            graphs=(surface.graph,),
            sources={sid: args.spec},
            title=f"{sid}: {candidate.producer} -> {candidate.target}",
            workflow_id=f"{candidate.producer}-to-{candidate.target}",
            refusal_reason=(f"no confident plan for '{candidate.target}' on '{sid}'"),
        )
        name = f"{sid}.{candidate.target}.arazzo.json"
        (out_dir / name).write_text(json.dumps(doc, indent=2), encoding="utf-8")
        written.append(name)
        if not is_executable(doc):
            refused += 1
        print(f"  {describe(candidate)}")

    _print_withheld(withheld)
    index = out_dir / f"{sid}.workflows.md"
    index.write_text(render_index(sid, candidates, written), encoding="utf-8")
    print(f"\n  → wrote {len(written)} Arazzo document(s) to {out_dir.resolve()}")
    print(f"  → wrote {index.resolve()}")
    if refused:
        # Refusals are the interesting half. Never let them pass as a silent success.
        print(
            f"  {refused} of {len(written)} could not be derived confidently and carry "
            "a refusal instead of a workflow (deliberately not Arazzo-valid).",
        )
    return 0


def _cmd_index(argv: list[str]) -> int:
    """`gecko index <specA> <specB> [...]` — the multi-provider value-domain index.

    Thin transport, like `gecko correlate`: build a Surface per spec, then report the
    deterministic ``entity -> {producers, consumers}`` map + the cross-provider joins as
    JSON. A cross-provider join is plan-eligible ONLY when both sides are customer-
    confirmed (§13.6); a provider-only declaration is a quarantined candidate. No vectors:
    the index is a strict lookup by DECLARED entity. This is a report — non-zero exit is
    not used.
    """
    from .access import public_session
    from .surface import Surface
    from .vindex import value_domain_index

    p = argparse.ArgumentParser(
        prog="gecko index",
        description="Report the value-domain index across N surfaces, with provenance.",
    )
    p.add_argument("specs", nargs="+", help="Two+ OpenAPI URLs, paths, or docs URLs.")
    args = p.parse_args(argv)
    if len(args.specs) < 2:
        print("index: need at least two specs to correlate.", file=sys.stderr)
        return 2

    surfaces = [
        Surface.from_spec(spec, session=public_session(), surface_id=f"s{i}")
        for i, spec in enumerate(args.specs)
    ]
    print(json.dumps(value_domain_index(surfaces).to_dict(), indent=2))
    return 0


def _parse_kv(pairs: list[str]) -> dict[str, str]:
    """``NAME=ENTITY`` CLI pairs -> a dict. Skips malformed entries (no ``=``)."""
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" in pair:
            name, _, entity = pair.partition("=")
            if name and entity:
                out[name] = entity
    return out


def _cmd_watch(argv: list[str]) -> int:
    """`gecko watch <plan.json>` — re-simulate a watch plan on a schedule.

    The scheduler half of drift: `gecko drift` reads a series someone else produced,
    `gecko watch` produces it. Thin transport over ``gecko.drift_watch`` — parse args,
    load the plan, run passes, print what the module formats. Exit codes: 0 = ran clean,
    1 = drift confirmed, 2 = the plan could not be read or every target failed.
    """
    from .drift_watch import (
        WatchError,
        confirm_unavailable,
        format_run,
        load_plan,
        run_once,
        watch,
    )

    p = argparse.ArgumentParser(
        prog="gecko watch",
        description=(
            "Re-simulate the calls in a watch plan and report N-confirmed drift. "
            "Every pass is simulateTransaction on an unsigned tx — $0, never signs, "
            "never broadcasts. Exit 0 = stable, 1 = drift confirmed."
        ),
    )
    p.add_argument(
        "plan", help="A watch-plan JSON file (local config; never transmitted)."
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit, instead of watching on the plan's interval.",
    )
    p.add_argument(
        "--passes",
        type=int,
        default=None,
        help="Stop after N passes (default: run until interrupted).",
    )
    args = p.parse_args(argv)

    try:
        plan = load_plan(args.plan)
    except WatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    drifted = False
    try:
        if args.once:
            runs = [run_once(plan)]
            for line in format_run(runs[0]):
                print(line)
        else:

            def _report(run: Any) -> None:
                for line in format_run(run):
                    print(line)

            runs = watch(plan, passes=args.passes, on_run=_report)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0

    if not runs:
        return 0

    # A call that can no longer be ASSEMBLED never reaches the simulator, so it records
    # no series row and detect_drift is structurally blind to it. Confirm it the same
    # way — by repetition across passes — and treat it as just as important: a call you
    # depend on that cannot be built is not a lesser break than one that reverts.
    stale = confirm_unavailable(runs, n_confirm=plan.n_confirm)
    for label in stale:
        print(f"  BROKEN  {label}: unrunnable for {plan.n_confirm} consecutive passes")

    drifted = bool(stale) or any(run.events for run in runs)
    if all(not r.ok for r in runs[-1].results):
        print("error: no target could be simulated", file=sys.stderr)
        return 2
    return 1 if drifted else 0


def _cmd_prove(argv: list[str]) -> int:
    """`gecko prove "<intent>"` — a sentence in, a receipt out.

    Thin transport over ``gecko.prove``: parse args, run it, print what the module
    formats. Exit codes: 0 = the call was proven to land, 1 = it was routed but did not
    pass, 2 = nothing could be routed.
    """
    from .prove import format_proof, prove
    from .rpc import LOCAL_RPC

    p = argparse.ArgumentParser(
        prog="gecko prove",
        description=(
            "Route an intent to the right call, show what the call needs and where "
            "each account came from, and simulate it before anything is signed. "
            "$0, unsigned, on a fork. Exit 0 = lands, 1 = routed but does not pass."
        ),
    )
    p.add_argument("intent", help="What you want to do, in plain language.")
    p.add_argument(
        "--bind",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="An input the call needs (repeatable), e.g. --bind user=<pubkey>.",
    )
    p.add_argument("--rpc-url", default=LOCAL_RPC, help="RPC to simulate against.")
    p.add_argument(
        "--accounts",
        type=int,
        default=0,
        metavar="N",
        help="Also list the first N accounts with their provenance.",
    )
    args = p.parse_args(argv)

    bindings: dict[str, Any] = {}
    for pair in args.bind:
        key, _, value = pair.partition("=")
        if not key or not _:
            print(f"error: --bind expects KEY=VALUE, got {pair!r}", file=sys.stderr)
            return 2
        bindings[key] = int(value) if value.isdigit() else value

    result = prove(args.intent, bindings=bindings, rpc_url=args.rpc_url)
    for line in format_proof(result, show_accounts=args.accounts):
        print(line)

    if result.start is None:
        return 2
    if result.receipt is None or result.receipt.status != "pass":
        return 1
    return 0


def _cmd_drift(argv: list[str]) -> int:
    """`gecko drift <path>` — read a simulated.jsonl and print N-confirmed drift.

    Thin transport over ``gecko.drift.detect_drift``: parse the segregated
    ``simulated.jsonl`` rows (fail-closed rehydration — a non-allowlisted or
    off-vocabulary row is rejected, never defaulted), detect categorical class
    changes per ``recipe_hash``, print them. Categorical output only. Exit codes:
    0 = no drift, 1 = drift detected, 2 = unreadable input.
    """
    from .corpus import CorpusError, simulated_outcome_from_record, simulated_sibling
    from .drift import detect_drift

    p = argparse.ArgumentParser(
        prog="gecko drift",
        description=(
            "Detect N-confirmed categorical outcome drift in a simulated corpus "
            "(the D2 series: same recipe_hash, changed (status, revert_class) "
            "across slots). Exit 0 = stable, 1 = drift detected."
        ),
    )
    p.add_argument(
        "path",
        help=(
            "A simulated.jsonl file, or a corpus path — its simulated.jsonl "
            "sibling is read (the same routing record_simulated writes with)."
        ),
    )
    p.add_argument(
        "--n-confirm",
        type=int,
        default=2,
        help="Distinct slots the new class must hold before it counts (default: 2).",
    )
    p.add_argument("--json", action="store_true", help="Emit the events as JSON.")
    args = p.parse_args(argv)

    path = Path(args.path)
    if path.name != "simulated.jsonl":
        path = simulated_sibling(path)
    if not path.exists():
        print(f"no simulated corpus at {path}", file=sys.stderr)
        return 2
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(simulated_outcome_from_record(json.loads(line)))
    except (CorpusError, json.JSONDecodeError, TypeError) as exc:
        print(f"unreadable simulated corpus: {exc}", file=sys.stderr)
        return 2

    events = detect_drift(rows, n_confirm=args.n_confirm)
    if args.json:
        from dataclasses import asdict

        print(json.dumps([asdict(ev) for ev in events], indent=2))
    else:
        recipes = len({row.recipe_hash for row in rows})
        if not events:
            print(f"no drift detected ({len(rows)} row(s), {recipes} recipe(s))")
        for ev in events:
            print(
                f"DRIFT {ev.recipe_hash[:12]} {ev.program_id} {ev.instruction}: "
                f"{ev.from_class} -> {ev.to_class} "
                f"(first seen slot {ev.first_seen_slot}, confirmed slot "
                f"{ev.confirmed_slot}, {ev.confirmations} confirmation(s))"
            )
    return 1 if events else 0


def _cmd_metrics(argv: list[str]) -> int:
    """`gecko metrics <spec>` — the Agent-Surface Report: comprehension, measured.

    Thin transport over ``gecko.metrics.compute_metrics``: prints the three
    provenance-labeled numbers (context-compression, surface-readiness, correlation).
    ``--peer`` adds another spec to count cross-provider joins against; ``--confirm
    NAME=ENTITY`` injects a customer-CONFIRMED value-domain hint (the join-plan
    gate). Honesty is law — every number is measured/derived, never fabricated.
    """
    from . import metrics as metricsmod
    from .access import public_session
    from .surface import Surface

    p = argparse.ArgumentParser(
        prog="gecko metrics",
        description="Measure comprehension: compression, readiness, correlation.",
    )
    p.add_argument("spec", help="An OpenAPI URL, path, or docs URL.")
    p.add_argument("--id", default=None, help="Surface id label (default: from host).")
    p.add_argument(
        "--peer",
        action="append",
        default=[],
        help="A peer spec to count cross-provider joins against (repeatable).",
    )
    p.add_argument(
        "--peer-id",
        action="append",
        default=[],
        help="Surface id for the matching --peer (repeatable, positional).",
    )
    p.add_argument(
        "--confirm",
        action="append",
        default=[],
        metavar="NAME=ENTITY",
        help="Inject a customer-CONFIRMED value-domain hint for the primary surface.",
    )
    p.add_argument(
        "--peer-confirm",
        action="append",
        default=[],
        metavar="NAME=ENTITY",
        help="Inject a customer-CONFIRMED value-domain hint for EVERY peer surface.",
    )
    p.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = p.parse_args(argv)

    spec = load_spec(args.spec)
    raw_source = None
    if not args.spec.startswith(("http://", "https://")):
        try:
            raw_source = Path(args.spec).read_text(encoding="utf-8")
        except OSError:
            raw_source = None

    peer_confirm = _parse_kv(args.peer_confirm)
    peers = []
    for i, peer_spec in enumerate(args.peer):
        pid = args.peer_id[i] if i < len(args.peer_id) else f"peer{i}"
        peers.append(
            Surface.of(
                AgentApiClient(
                    peer_spec,
                    session=public_session(),
                    surface_id=pid,
                    declared_hints=peer_confirm or None,
                )
            )
        )

    m = metricsmod.compute_metrics(
        spec,
        raw_source=raw_source,
        surface_id=args.id,
        declared_hints=_parse_kv(args.confirm) or None,
        peers=peers,
    )
    if args.json:
        print(json.dumps(m.to_dict(), indent=2))
        return 0
    _print_metrics(m)
    return 0


def _print_metrics(m: Any) -> None:
    """Human-readable Agent-Surface Report — every number labeled by provenance."""
    c, r, x = m.compression, m.readiness, m.correlation
    print(f"Agent-Surface Report — {m.surface_id}  ({m.total_ops} operations)")
    print("")
    print("  context-compression (bytes measured, tokens estimated)")
    print(f"    raw OpenAPI:     {c.raw_bytes:>9,} B  (~{c.raw_tokens_est:,} tok est)")
    print(
        f"    agent surface:   {c.surface_bytes:>9,} B  (~{c.surface_tokens_est:,} tok est)"
    )
    if c.is_enriched:
        # too-sparse spec: comprehension ADDED calling context (surface larger, honest).
        print(
            f"    enrichment:      +{c.magnitude_pct:>7}%  richer — spec too sparse to "
            f"call correctly; Gecko added the comprehension ({c.token_estimate})"
        )
    else:
        print(f"    reduction:       {c.reduction_pct:>8}%  ({c.token_estimate})")
    print("")
    print("  surface-readiness (recorded, NOT live-verified)")
    print(
        f"    well-formed:     {r.well_formed_tools}/{r.total_ops} tools  "
        f"({r.readiness_pct}%)   quarantined: {r.quarantined_tools}"
    )
    print("")
    print("  correlation (DECLARED value-domain; cross-joins CONFIRMED-only)")
    print(
        f"    declared entities:  {x.declared_entities}"
        + (f"  {list(x.entities)}" if x.entities else "")
    )
    print(f"    id-shaped join fields: {x.id_shaped_join_fields}")
    print(
        f"    self-declared domains: {x.self_declared_domains}"
        + (f"  {list(x.domains)}" if x.domains else "")
        + "  (corroborator only)"
    )
    if m.peers:
        print(
            f"    cross-provider joins vs {list(m.peers)}: "
            f"{x.cross_joins_plan_eligible} plan-eligible (confirmed), "
            f"{x.cross_joins_candidate} candidate (quarantined)"
        )


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
    print(
        "  scan-image <path>  scan an image for an image-borne injection (Skill Guard)"
    )
    print("  scan-doc <path>    scan an untrusted doc/convention page (Skill Guard)")
    print(
        '  prove "<intent>"   route an intent -> the call -> a receipt ($0, unsigned)'
    )
    print("  watch <plan>       re-simulate a watch plan on a schedule (CI)")
    print("  drift <path>       N-confirmed drift over a simulated.jsonl corpus")
    print(
        "  export-arazzo <spec>...  the derived plan as a portable Arazzo 1.0 doc "
        "(names only, no values)"
    )
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
    p.add_argument(
        "--probe",
        action="store_true",
        help="Self-test: connect, list tools, print the result, and EXIT (does not serve). "
        "Use this to verify from a terminal — plain `connect` is a server that waits for "
        "an MCP client, so it looks 'stuck' when run by hand.",
    )
    args = p.parse_args(argv)

    if args.probe:
        try:
            name, version, count = connect_mod.probe(args.surface, host=args.host)
        except connect_mod.ConnectError as exc:
            print(f"  ✗ {exc}", file=sys.stderr)
            return 2
        print(
            f"  ✓ connected to {name} {version} — {count} tools. "
            f"The key resolved, the host was reached, and auth passed.",
            file=sys.stderr,
        )
        return 0

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

    # Declare this process a LOCAL client so every usage event it emits is attributable
    # to one install instead of landing anonymous.
    #
    # Explicitly NOT for `serve`. `gecko serve` is the same entry point that runs the
    # HOSTED surface, so declaring an identity before dispatch would stamp the server's
    # own machine id onto every visitor's event and collapse every user into one — the
    # precise failure this seam exists to prevent, and worse than counting nothing.
    # Identity is declared for local, single-operator commands only.
    if cmd != "serve":
        try:
            events.set_local_install_id(telemetry.read_or_create_install_id())
        except Exception:  # noqa: BLE001 - identity is metadata, never a hard dependency
            pass
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
    if cmd == "watch":
        return _cmd_watch(rest)
    if cmd == "prove":
        return _cmd_prove(rest)
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
    if cmd == "orquestra":
        from .providers.cli import main as orquestra_main  # lazy: serve+solana deps

        return orquestra_main(rest)
    if cmd == "test":
        return _cmd_test(rest)
    if cmd == "inspect":
        return _cmd_inspect(rest)
    if cmd == "report":
        return _cmd_report(rest)
    if cmd == "verify-docs":
        return _cmd_verify_docs(rest)
    if cmd == "from-docs":
        return _cmd_from_docs(rest)
    if cmd == "scan-image":
        return _cmd_scan_image(rest)
    if cmd == "scan-doc":
        return _cmd_scan_doc(rest)
    if cmd == "auth":
        return _cmd_auth(rest)
    if cmd == "graph":
        return _cmd_graph(rest)
    if cmd == "correlate":
        return _cmd_correlate(rest)
    if cmd == "export-arazzo":
        return _cmd_export_arazzo(rest)
    if cmd == "workflows":
        return _cmd_workflows(rest)
    if cmd == "index":
        return _cmd_index(rest)
    if cmd == "metrics":
        return _cmd_metrics(rest)
    if cmd == "drift":
        return _cmd_drift(rest)
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
