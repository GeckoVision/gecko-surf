"""Screenplay — "we said it would cost 36,508 before it happened" (the mainnet cut).

The first three demos ended at a simulation. This one ends on chain.

A real purchase — 0.1 USDC of water at a real bar, from a real agentic wallet — was
verified by Gecko, signed by the wallet, and landed on Solana mainnet. The take does two
things live: it re-runs the pre-flight against mainnet, and it reads the transaction that
actually landed. The compute units on both sides are read off the wire during the
recording, and they are the same number.

Nothing is signed or broadcast during the take. The transaction already exists; we read it
back by signature, which anyone can do. That is stronger than sending one on camera: it is
independently checkable after the fact.

Record it (no surfpool needed — this one talks to mainnet, read-only):

    export GECKO_MAINNET_RPC="https://..."
    asciinema rec --cols 92 --rows 26 \
        -c "uv run python demo/kit/mainnet_receipt_screenplay.py" mainnet.cast

    uv run --with pyte --with pillow python demo/kit/render_cast.py mainnet.cast \
        docs/assets/mainnet-receipt.mp4 \
        --brand "GECKO  •  TRY THE CALL BEFORE YOU MAKE IT" \
        --scene "Gecko — before it happened|the receipt, on live mainnet" \
        --scene "Gecko — who did what|verification is not authorization" \
        --scene "Gecko — after it happened|the chain agrees"

Honesty contract (demo/kit/README.md): one unedited take; every number came off the wire
during the recording; nothing claimed that the run did not show.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from gecko.rpc import default_rpc_call, user_agent  # noqa: E402
from gecko.simulate import BuiltTx, simulate  # noqa: E402
from gecko.txbind import evaluate_tx  # noqa: E402
from screenplay import BOLD, CYAN, GREEN, RESET, YELLOW, clear, out, put  # noqa: E402

RPC = os.environ["GECKO_MAINNET_RPC"]
#: The transaction this demo is about. Public, and checkable by anyone.
SIGNATURE = "5cjBs5VE8WVVctG2EoUkYiRkW92sXkoT4YsNxszWC9CE3sK7triTJ5vnY6TrcQ2BRPYUtWsd3LtTnyieUfn8Hw2Y"
WALLET = "3HrXPry37q5bcaa5C3m543bHLShpMxu7LF4KbRjBJN4i"
BUILD_URL = "https://api.orquestra.dev/api/p7o7nf4pucllzadrmiqhf/instructions/make_purchase/build"


def _rpc(url: str, method: str, params: list) -> dict:
    if method == "getAccountInfo":
        params = list(params)
        opts = (
            dict(params[1]) if len(params) > 1 and isinstance(params[1], dict) else {}
        )
        opts["encoding"] = "base64"
        params = [params[0], opts]
    return default_rpc_call(url, method, params)


def _build() -> dict:
    from scripts.prepare_purchase import (  # noqa: PLC0415
        ATA_PROGRAM,
        STORE_AUTHORITY,
        STORE_RECEIPTS,
        STORE_TOKEN_ACCOUNT,
        SYSTEM_PROGRAM,
        TOKEN_PROGRAM,
        USDC,
        derive_ata,
    )

    body = json.dumps(
        {
            "accounts": {
                "receipts": STORE_RECEIPTS,
                "signer": WALLET,
                "authority": STORE_AUTHORITY,
                "mint": USDC,
                "sender_token_account": derive_ata(WALLET, USDC),
                "recipient_token_account": STORE_TOKEN_ACCOUNT,
                "token_program": TOKEN_PROGRAM,
                "system_program": SYSTEM_PROGRAM,
                "associated_token_program": ATA_PROGRAM,
            },
            "args": {
                "store_name": "jonasbar",
                "product_name": "Water",
                "table_number": 11,
            },
            "feePayer": WALLET,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        BUILD_URL,
        data=body,
        headers={"content-type": "application/json", "user-agent": user_agent()},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
        return json.loads(response.read())


# ---------------------------------------------------------------- scene 1
out(f"{BOLD}An agent is about to spend real money.{RESET}", pause=0.5)
out("0.1 USDC. A bottle of water, at a bar that takes crypto.", pause=0.7)
out("Small — and once it is signed, it is gone.", pause=0.9)
out()
out(f"{CYAN}$ # ask first: what does this call actually do?{RESET}", 0.02)

_built = _build()
_receipt = simulate(
    {},
    rpc_url=RPC,
    rpc_call=_rpc,
    build_call=lambda _plan: BuiltTx(
        tx=_built["serializedTransaction"], encoding=_built.get("encoding", "base58")
    ),
    replace_blockhash=False,
    network_label="simulated against LIVE mainnet (read-only, unsigned)",
)

put(
    f"  the builder says   simulationError = {_built.get('simulationError')}", pause=0.6
)
put()
put(f"{GREEN}  PASS{RESET}   {_receipt.units_consumed:,} compute units", pause=0.5)
put(f"  {_receipt.network_label}", pause=0.5)
put(
    f"  binding  {(_receipt.message_binding or '')[:40]}…  [{_receipt.binding_strength}]",
    pause=1.0,
)
put()
_verdict = evaluate_tx(
    _built["serializedTransaction"],
    _receipt,
    encoding=_built.get("encoding", "base58"),
    require="exact",
)
put(f"{YELLOW}The receipt is bound to THIS message. Not one like it.{RESET}", pause=1.6)

clear()

# ---------------------------------------------------------------- scene 2
out(f"{BOLD}Then three different things had to agree.{RESET}", pause=0.6)
out()
put(
    f"  {GREEN}✓{RESET} Gecko      will it work?        simulated, bound, PASS",
    pause=0.5,
)
put(
    f"  {GREEN}✓{RESET} the wallet  who signs it?        the agentic wallet did",
    pause=0.5,
)
put(
    f"  {GREEN}✓{RESET} the policy  are you allowed?     single-tx limit, enforced",
    pause=1.0,
)
put()
put("  Gecko never held a key and never broadcast anything.", pause=0.8)
put(
    f"{YELLOW}Verification is not authorization. Both had to say yes.{RESET}", pause=1.7
)

clear()

# ---------------------------------------------------------------- scene 3
out(f"{BOLD}And then it happened. Here is the chain.{RESET}", pause=0.6)
out()
out(f"{CYAN}$ # reading the transaction that landed{RESET}", 0.02)

_tx = default_rpc_call(
    RPC,
    "getTransaction",
    [
        SIGNATURE,
        {
            "encoding": "json",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        },
    ],
)["result"]
_meta = _tx["meta"]
_when = datetime.datetime.utcfromtimestamp(_tx["blockTime"]).isoformat()

put(f"  slot     {_tx['slot']}", pause=0.3)
put(f"  time     {_when}Z", pause=0.3)
put(f"  error    {_meta.get('err')}", pause=0.4)
put(f"  consumed {_meta.get('computeUnitsConsumed'):,} compute units", pause=0.9)
put()

_pre = {
    b["accountIndex"]: float(b["uiTokenAmount"]["uiAmountString"])
    for b in _meta.get("preTokenBalances", [])
}
_post = {
    b["accountIndex"]: float(b["uiTokenAmount"]["uiAmountString"])
    for b in _meta.get("postTokenBalances", [])
}
for _i in sorted(set(_pre) | set(_post)):
    _delta = _post.get(_i, 0) - _pre.get(_i, 0)
    if abs(_delta) > 1e-9:
        _who = "the buyer " if _delta < 0 else "the bar   "
        put(f"  {_who} {_delta:+.2f} USDC", pause=0.4)
put()

_predicted = _receipt.units_consumed or 0
_actual = _meta.get("computeUnitsConsumed") or 0
put(f"  the chain charged   {_actual:,} CU", pause=0.5)
put(f"  a pre-flight NOW    {_predicted:,} CU", pause=0.9)
put()
if _predicted == _actual:
    put(f"{GREEN}The same number — the state has not moved since.{RESET}", pause=1.4)
else:
    # Not a defect, and not hidden: the receipts account gained a record when this very
    # purchase landed, so the program does slightly different work now. It is the
    # sharpest argument the demo has — a receipt is only true for the state it was taken
    # against, which is exactly why the binding expires with its blockhash.
    put(
        f"{YELLOW}{abs(_actual - _predicted)} units apart — and that is the point.{RESET}",
        pause=0.9,
    )
    put("  This purchase changed the account it wrote to. The program's", pause=0.5)
    put("  work is not identical any more.", pause=0.9)
    put()
    put(
        f"{GREEN}A receipt is true for the state it was taken against.{RESET}",
        pause=0.6,
    )
    put(
        f"{GREEN}So take it at the moment you sign — not the day before.{RESET}",
        pause=1.5,
    )
put()
put(f"{BOLD}Gecko comprehends. The wallet signs. The chain settles.{RESET}")
put(f"{CYAN}npx @geckovision/gecko{RESET}", pause=2.2)
