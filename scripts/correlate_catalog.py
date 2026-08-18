"""What actually correlates across a catalogue of Solana programs?

The claim worth testing is our own: a join basis is a VALUE DOMAIN, never a name. So this
measures both, side by side — how often an account name recurs across programs, and how often
the recurrence is a genuine join rather than a homonym.

Read-only. Fetches IDLs from a public catalogue and builds our own graph over each.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter, defaultdict

sys.path.insert(0, "/home/nan/PycharmProjects/Gecko/surfcall")

from gecko.program_graph import build_program_graph

UA = {"User-Agent": "gecko-surf/correlation-study"}
BASE = "https://api.orquestra.dev"

#: Addresses every program shares because the runtime does, not because they are related.
#: Counting these as correlation would be counting the floor.
UNIVERSAL = {
    "11111111111111111111111111111111": "system",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "spl-token",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "associated-token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "token-2022",
    "SysvarRent111111111111111111111111111111111": "rent",
    "Sysvar1nstructions1111111111111111111111111": "instructions",
    "ComputeBudget111111111111111111111111111111": "compute-budget",
}


def idl_of(project_id: str) -> dict | None:
    try:
        request = urllib.request.Request(f"{BASE}/api/idl/{project_id}", headers=UA)
        with urllib.request.urlopen(request, timeout=45) as response:
            body = json.loads(response.read())
    except Exception:
        return None
    value = body.get("idl") or body
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    return value if isinstance(value, dict) and "instructions" in value else None


projects = json.load(open("/tmp/projects.json"))["projects"]

name_programs: dict[str, set[str]] = defaultdict(
    set
)  # account name -> programs using it
name_kinds: dict[str, Counter] = defaultdict(Counter)  # account name -> pda? / plain?
seed_programs: dict[bytes, set[str]] = defaultdict(set)  # const seed bytes -> programs
pinned: Counter = Counter()  # pinned address -> programs
arg_types: dict[str, Counter] = defaultdict(Counter)  # arg name -> declared types
built = 0

for project in projects:
    idl = idl_of(project["id"])
    if not idl:
        continue
    try:
        graph = build_program_graph(idl=idl, program_id=project.get("program_id"))
    except Exception:
        continue
    built += 1
    name = project["name"]

    for instruction in graph.instructions:
        for arg_name, arg_type in instruction.args:
            arg_types[arg_name][str(arg_type)] += 1
        for account in instruction.accounts:
            name_programs[account.name].add(name)
            name_kinds[account.name]["pda" if account.is_pda else "plain"] += 1

    for node in graph.pdas.values():
        for seed in node.seeds:
            value = getattr(seed, "value", None)
            if isinstance(value, (bytes, bytearray)) and 2 <= len(value) <= 32:
                seed_programs[bytes(value)].add(name)

    # pinned addresses an instruction hardcodes = a real cross-program edge
    for instruction in idl.get("instructions", []):
        for account in instruction.get("accounts", []):
            address = account.get("address")
            if isinstance(address, str):
                pinned[address] += 1

print(f"graphs built: {built} of {len(projects)}\n")

print("=" * 74)
print("1. ACCOUNT NAMES THAT RECUR ACROSS PROGRAMS — the tempting join, and the trap")
print("=" * 74)
shared = sorted(name_programs.items(), key=lambda kv: -len(kv[1]))
for account_name, programs in shared[:14]:
    kinds = name_kinds[account_name]
    mixed = kinds["pda"] > 0 and kinds["plain"] > 0
    flag = "  <-- SAME NAME, DIFFERENT THING" if mixed else ""
    print(
        f"  {account_name:26} {len(programs):3} programs   pda={kinds['pda']:4} plain={kinds['plain']:4}{flag}"
    )

multi = [n for n, p in name_programs.items() if len(p) > 1]
homonym = [n for n in multi if name_kinds[n]["pda"] and name_kinds[n]["plain"]]
print(f"\n  account names used by >1 program : {len(multi)}")
print(
    "  of those, the SAME NAME is a PDA in one program and a plain account in another:"
)
print(
    f"     {len(homonym)}  ({len(homonym) * 100 // max(len(multi), 1)}%) — a name join would fuse these"
)

print("\n" + "=" * 74)
print("2. WHAT PROGRAMS ACTUALLY SHARE — pinned addresses (a real CPI edge)")
print("=" * 74)
for address, count in pinned.most_common(10):
    label = UNIVERSAL.get(address, "")
    marker = (
        f"  [{label}]" if label else "  <-- NOT universal: a genuine shared dependency"
    )
    print(f"  {address[:44]:46} {count:4}{marker}")

print("\n" + "=" * 74)
print("3. SEED VOCABULARY SHARED ACROSS PROGRAMS")
print("=" * 74)
shared_seeds = sorted(
    ((s, p) for s, p in seed_programs.items() if len(p) > 1), key=lambda kv: -len(kv[1])
)
for seed, programs in shared_seeds[:12]:
    try:
        text = seed.decode()
    except UnicodeDecodeError:
        text = seed.hex()[:20]
    print(
        f"  {text!r:26} {len(programs):3} programs   {', '.join(sorted(programs))[:60]}"
    )
print(f"\n  distinct const seeds seen in >1 program: {len(shared_seeds)}")

print("\n" + "=" * 74)
print("4. ARGUMENT NAMES WHOSE TYPE DISAGREES ACROSS PROGRAMS")
print("=" * 74)
disagree = [(n, t) for n, t in arg_types.items() if len(t) > 1 and sum(t.values()) > 3]
for arg_name, types in sorted(disagree, key=lambda kv: -sum(kv[1].values()))[:10]:
    shown = ", ".join(f"{t}×{c}" for t, c in types.most_common(3))
    print(f"  {arg_name:22} {shown}")
print(f"\n  argument names carrying more than one declared type: {len(disagree)}")
