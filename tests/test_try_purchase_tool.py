"""``try_purchase`` as an agent reaches it — the tool, and the order it does things in.

The rehearsal itself is pinned in ``tests/test_sandbox_rehearse.py``; this file is about
the thing mounted on a PUBLIC surface. Two properties matter more than the rest, and both
are written as mutations someone could actually make:

* **no key exists until the fork has proved itself.** Move ``ephemeral_signer`` above
  ``prove_surfnet`` and ``test_no_key_is_created_until_the_endpoint_proves_itself`` goes
  red — the spy records a signer for an endpoint that answered nothing.
* **the endpoint must be on this machine.** Drop :func:`_local_fork_refusal` and
  ``test_a_remote_endpoint_is_refused_before_the_first_round_trip`` goes red — a stranger
  on the hosted mount gets the server to POST to an address of their choosing.

Nothing here touches a validator except the ``fork``-marked leg, which starts a local
surfpool fork and buys something on it for real. Deselect with ``-m "not fork"``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from gecko.pda_testkit import SurfpoolFork
from gecko.providers.catalog_surface import OrquestraCatalogSurface
from gecko.rpc import RpcError
from gecko.sandbox import try_purchase as module
from gecko.sandbox.rehearse import Rehearsal, Refusal
from gecko.sandbox.try_purchase import (
    _pin_localhost,
    HOW_TO_START_A_FORK,
    TRY_PURCHASE_TOOL,
    _local_fork_refusal,
    _to_json,
    try_purchase_result,
)
from tests.test_sandbox_rehearse import (
    MEASURED_INFO_RESULT,
    USDC,
    FakeFork,
    fresh_pubkey,
    needs_fork,
    offline_proof,
    wired_fake,
)

FORK_PORT = 8938
LOCAL = "http://127.0.0.1:8899"


def proving_fake(store: str, authority: str, product: str, price: int) -> FakeFork:
    """The rehearsal's fake fork, taught to answer the ONE method that gates a key.

    Kept as a separate helper rather than folded into ``wired_fake``: the fake that does
    NOT answer it is what most of ``test_sandbox_rehearse`` uses, and a fake that always
    proved itself would quietly weaken every test that depends on it not doing so.
    """
    fork = wired_fake(store, authority, product, price)
    fork._surfnet_getSurfnetInfo = lambda _params: MEASURED_INFO_RESULT  # type: ignore[attr-defined]
    return fork


class NoSurfnet:
    """An endpoint that answers like a real network: it has never heard of the cheatcode.

    Records every request so a test can assert what was NOT sent, which is the half that
    matters for an ordering guarantee.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def __call__(self, url: str, method: str, params: list[Any]) -> dict[str, Any]:
        self.seen.append((url, method))
        raise RpcError(f"{method} failed: code=-32601 message='Method not found'")


@pytest.fixture
def key_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ephemeral key this tool creates. Empty is a claim, not an absence."""
    created: list[str] = []
    real = module.ephemeral_signer

    def spy(proof: Any) -> Any:
        signer = real(proof)
        created.append(signer.pubkey)
        return signer

    monkeypatch.setattr(module, "ephemeral_signer", spy)
    return created


# ---------------------------------------------------------------------------
# the guard: nothing exists before the proof
# ---------------------------------------------------------------------------


#: Every test below authenticates, because `try_purchase` is account-gated and these are
#: testing the TOOL rather than the gate. The one test that must stay anonymous is
#: `test_an_anonymous_caller_gets_no_rehearsal_and_no_key`, which calls the real function
#: directly — if this helper ever grows a default that fills the account in, that test
#: stops testing anything.
def as_account(arguments: dict, **kw: object) -> dict:
    """Call the tool as an authenticated caller."""
    return try_purchase_result(arguments, account="acct_test", **kw)  # type: ignore[arg-type]


def test_no_key_is_created_until_the_endpoint_proves_itself(key_spy: list[str]) -> None:
    """The whole point of the tool, expressed as an order of operations.

    An RPC that does not implement ``surfnet_getSurfnetInfo`` is mainnet, or something
    pretending to be a fork. The refusal has to land before a secret exists — noticing
    afterwards means a key was created for an endpoint that proved nothing.
    """
    fork = NoSurfnet()
    out = as_account(
        {"store": "teststore", "product": "Water", "rpc_url": LOCAL}, rpc_call=fork
    )

    assert out["refused"] is True
    assert out["code"] == "no-fork-proved"
    assert key_spy == [], "a key was created for an endpoint that proved nothing"
    # It asked exactly one question, and it was the proof.
    assert [method for _url, method in fork.seen] == ["surfnet_getSurfnetInfo"]


def test_the_refusal_names_the_command_that_makes_a_fork() -> None:
    """A refusal an agent cannot act on is a shrug. This one is a command to run."""
    out = as_account(
        {"store": "teststore", "product": "Water", "rpc_url": LOCAL},
        rpc_call=NoSurfnet(),
    )

    assert "surfpool start --no-tui --no-deploy" in out["reason"]
    assert HOW_TO_START_A_FORK in out["reason"]
    # and it says, in the words an agent reads, that there is no other route
    assert "no path that falls back to mainnet" in out["reason"]
    assert "NOTHING WAS SIGNED, SENT OR EVEN PREPARED" in out["reason"]


def test_a_remote_endpoint_is_refused_before_the_first_round_trip(
    key_spy: list[str],
) -> None:
    """This tool signs, so it may only ever talk to a fork beside the process.

    The surface it mounts on is public and unauthenticated. Without this, a stranger
    supplies an endpoint of their own — one that happily answers the cheatcode — and the
    server signs and sends for a network nobody here chose.
    """
    for elsewhere in (
        "http://10.0.0.5:8899",  # RFC1918: reachable from a hosted box, not ours
        "http://169.254.169.254/latest",  # cloud metadata
        "http://8.8.8.8:8899",  # a public address
        "ftp://127.0.0.1:8899",  # not even http
    ):
        fork = NoSurfnet()
        out = as_account(
            {"store": "teststore", "product": "Water", "rpc_url": elsewhere},
            rpc_call=fork,
        )
        assert out["refused"] is True, elsewhere
        assert out["code"] == "not-a-local-fork", elsewhere
        assert fork.seen == [], f"{elsewhere} was contacted before it was judged"
    assert key_spy == []


def test_the_local_check_admits_loopback_in_every_spelling() -> None:
    """Including the IPv4-mapped IPv6 form, which is loopback and reads like it is not."""
    for local in (
        "http://127.0.0.1:8899",
        "http://127.0.0.2:8899",
        "http://[::1]:8899",
        "http://[::ffff:127.0.0.1]:8899",
        "https://127.0.0.1:8899",
    ):
        assert _local_fork_refusal(local) is None, local
    for remote in ("http://192.168.1.10:8899", "http://[2001:4860:4860::8888]:8899"):
        assert _local_fork_refusal(remote) is not None, remote


# ---------------------------------------------------------------------------
# the arguments
# ---------------------------------------------------------------------------


def test_it_never_defaults_an_rpc_url(key_spy: list[str]) -> None:
    """No `rpc_url` means no fork, and a default here would be a guess about which chain
    to spend on — which is the one guess this whole package exists to refuse."""
    out = as_account({"store": "teststore", "product": "Water"})
    assert out["code"] == "argument-invalid"
    assert "no default" in out["reason"]
    assert HOW_TO_START_A_FORK in out["reason"]
    assert key_spy == []


def test_a_supplied_buyer_is_refused_rather_than_ignored(key_spy: list[str]) -> None:
    """`buyer` is the field an agent copies across from `prepare_purchase`.

    Accepting it and then paying from somewhere else would make the response a lie about
    whose money moved — worse than the refusal, because it looks like it worked.
    """
    out = as_account(
        {
            "store": "teststore",
            "product": "Water",
            "rpc_url": LOCAL,
            "buyer": fresh_pubkey(),
        },
        rpc_call=NoSurfnet(),
    )
    assert out["code"] == "argument-invalid"
    assert "takes no `buyer`" in out["reason"]
    assert "prepare_purchase" in out["reason"]
    assert key_spy == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"product": "Water", "rpc_url": LOCAL},
        {"store": "   ", "product": "Water", "rpc_url": LOCAL},
        {"store": "teststore", "rpc_url": LOCAL},
        {"store": "teststore", "product": "Water", "rpc_url": LOCAL, "table": 999},
        {"store": "teststore", "product": "Water", "rpc_url": LOCAL, "table": "7"},
    ],
)
def test_bad_arguments_refuse_before_anything_is_proved(
    arguments: dict[str, Any], key_spy: list[str]
) -> None:
    fork = NoSurfnet()
    out = as_account(arguments, rpc_call=fork)
    assert out["code"] == "argument-invalid"
    assert fork.seen == []
    assert key_spy == []


# ---------------------------------------------------------------------------
# what comes back
# ---------------------------------------------------------------------------


def test_every_response_carries_the_sandbox_marker() -> None:
    """Success, refusal and blocked alike. An agent that only ever sees a refusal must
    still be able to tell this tool had nothing to do with mainnet."""
    responses = [
        as_account({}),
        as_account({"store": "s", "product": "p"}),
        as_account({"store": "s", "product": "p", "rpc_url": "http://8.8.8.8"}),
        as_account(
            {"store": "s", "product": "p", "rpc_url": LOCAL}, rpc_call=NoSurfnet()
        ),
        _to_json(_landed_rehearsal(), proof=offline_proof(LOCAL)),
    ]
    assert all(response["sandbox"] is True for response in responses)


def _landed_rehearsal() -> Rehearsal:
    from gecko.sandbox.rehearse import LamportDelta, TokenDelta, WrittenReceipt

    return Rehearsal(
        store="teststore",
        product="Water",
        price_raw=100_000,
        mint=USDC,
        buyer="BuyerPubkey",
        landed=True,
        signature="5jwHoU3",
        simulated_units=42_494,
        units_consumed=42_494,
        fee_lamports=5_000,
        buyer_token=TokenDelta(
            account="ata", owner="BuyerPubkey", mint=USDC, before=100_000, after=0
        ),
        store_token=TokenDelta(
            account="store_ata",
            owner="Authority",
            mint=USDC,
            before=21_630_226,
            after=21_730_226,
        ),
        buyer_sol=LamportDelta(
            address="BuyerPubkey", before=50_000_000, after=49_995_000
        ),
        receipt=WrittenReceipt(
            receipt_id=127,
            product_name="Water",
            price_raw=100_000,
            table_number=7,
            delivered=False,
            total_purchases_before=127,
            total_purchases_after=128,
        ),
        window_blocks=151,
        reset=("addr: 1 -> None lamports",),
    )


def test_the_projection_carries_the_deltas_and_the_caveats() -> None:
    """What an agent reads is what the chain showed — plus what it still does not prove."""
    out = _to_json(_landed_rehearsal(), proof=offline_proof(LOCAL))

    assert out["refused"] is False
    assert out["network"] == "fork"
    assert out["proved"]["method"] == "surfnet_getSurfnetInfo"
    assert out["landed"] is True and out["balanced"] is True
    # `moved` is carried, not left to be subtracted: None on a side means "no account",
    # which is not zero, and an agent doing the arithmetic would erase that.
    assert out["moved"]["buyer_token"]["moved"] == -100_000
    assert out["moved"]["store_token"]["moved"] == +100_000
    assert out["moved"]["buyer_sol"]["moved"] == -5_000
    assert out["receipt"]["receipt_id"] == 127
    assert out["compute"] == {
        "simulated_units": 42_494,
        "units_consumed": 42_494,
        "agree": True,
    }
    # The honesty payload rides in the RESULT, because a description is read once and a
    # result is read every time.
    caveats = " ".join(out["what_this_does_not_prove"])
    assert "NOT A COMPUTE ORACLE" in caveats
    assert (
        "36,399" in caveats and "42,494" in caveats
    )  # the measured pair, not a slogan
    assert "rent" in caveats.lower() and "window" in caveats.lower()


def test_a_step_that_declined_reads_as_a_refusal_at_the_top_level() -> None:
    """A funded run that the production path refused is a refusal, in the same word the
    other tools use — with the whole rehearsal still underneath it."""
    declined = Rehearsal(
        store="teststore",
        product="Water",
        price_raw=100_000,
        mint=USDC,
        buyer="BuyerPubkey",
        landed=False,
        refusals=(
            Refusal("prepare", "[build-returned-nothing] the builder gave none"),
        ),
        reset=("addr: 1 -> None lamports",),
    )
    out = _to_json(declined, proof=offline_proof(LOCAL))

    assert out["refused"] is True
    assert out["code"] == "rehearsal-refused"
    assert "[prepare]" in out["reason"] and "build-returned-nothing" in out["reason"]
    # not smoothed into a failed ledger: nothing landed, so there is no verdict to give
    assert out["landed"] is False and out["balanced"] is None
    assert out["reset"], "the cleanup still happened and is still reported"


def test_nothing_shaped_like_a_secret_can_ride_out_in_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: the reasons come from other modules, and one of them could one
    day echo its input. Every string leaving here is redacted by PATTERN first."""
    leak = "4NfVfsHVeFbWFfPzAsRPQxvZAeGZjKsY7Q4tPCsyPUsC9pYq3TeoK1J1SPUKMcNn"
    out = _to_json(
        Rehearsal(
            store="s",
            product="p",
            price_raw=1,
            mint=USDC,
            buyer="BuyerPubkey",
            landed=False,
            refusals=(Refusal("prepare", f"the signer said {leak}"),),
            reset=(f"reset failed: {leak}",),
        ),
        proof=offline_proof(LOCAL),
    )

    assert leak not in str(out)
    assert "<redacted>" in out["reason"]
    assert "<redacted>" in out["reset"][0]


# ---------------------------------------------------------------------------
# the tool as it is mounted
# ---------------------------------------------------------------------------


def test_the_tool_definition_says_the_four_things_it_must() -> None:
    """The description is the only thing an agent reads before choosing. It has to state
    that this rehearses, that it LANDS for real there, that it cannot reach mainnet, and
    that the compute number is not mainnet's."""
    description = TRY_PURCHASE_TOOL["description"]

    assert "REHEARSE" in description
    assert "LANDS FOR REAL ON THAT FORK" in description
    assert "CAN NEVER TOUCH MAINNET" in description
    assert "A FORK IS NOT A COMPUTE ORACLE" in description
    assert "surfnet_getSurfnetInfo" in description
    # and where to go when the human actually wants the product
    assert "prepare_purchase" in description

    schema = TRY_PURCHASE_TOOL["inputSchema"]
    assert sorted(schema["required"]) == ["product", "rpc_url", "store"]
    # the same inputs as prepare_purchase, plus the fork — and no `buyer`, because the
    # buyer is created inside the call
    assert sorted(schema["properties"]) == ["product", "rpc_url", "store", "table"]
    assert schema["additionalProperties"] is False
    assert HOW_TO_START_A_FORK in schema["properties"]["rpc_url"]["description"]


def test_only_the_tool_that_can_spend_got_a_twin() -> None:
    """The rule that keeps the tool count from doubling: `list_stores`, `find_start` and
    `comprehend_program` are already free and already safe."""
    names = [tool["name"] for tool in OrquestraCatalogSurface().list_tools()]
    assert names.index("try_purchase") == names.index("prepare_purchase") + 1
    assert [name for name in names if name.startswith("try_")] == ["try_purchase"]


def test_the_surface_routes_the_tool_to_the_rehearsal() -> None:
    """Wired is not reached. This drives `call_tool`, the way a client does.

    Also pins that the surface passes `account` THROUGH: without it the call stops at the
    gate and this test would go green on a refusal that never touched the rehearsal —
    the routing assertion satisfied by the tool never running.
    """
    surface = OrquestraCatalogSurface(purchase_rpc_call=NoSurfnet())
    out = surface.call_tool(
        "try_purchase",
        {"store": "teststore", "product": "Water", "rpc_url": LOCAL},
        account="acct_test",
    )
    assert out["sandbox"] is True
    assert out["code"] == "no-fork-proved"


def test_the_public_unauthenticated_mount_does_not_offer_the_rehearsal() -> None:
    """`call_tool` with no account is exactly what the public mount does today."""
    surface = OrquestraCatalogSurface(purchase_rpc_call=NoSurfnet())
    out = surface.call_tool(
        "try_purchase", {"store": "teststore", "product": "Water", "rpc_url": LOCAL}
    )
    assert out["code"] == "account-required"


def test_an_unknown_store_refuses_with_the_menu_pointer() -> None:
    """The store is read off the fork, so a name that is not there refuses exactly as it
    does on the real tool — after the proof, and with no transaction."""
    fork = proving_fake("teststore", fresh_pubkey(), "Water", 100_000)
    out = as_account(
        {"store": "nosuchstore", "product": "Water", "rpc_url": fork.rpc_url},
        rpc_call=fork,
    )
    assert out["refused"] is True
    assert out["code"] == "store-unknown"
    assert "list_stores" in out["reason"]
    assert "sendTransaction" not in {method for _url, method in fork.seen}


def test_a_product_the_store_does_not_sell_refuses_with_the_menu() -> None:
    """The price is a per-product fact in the store's own account, so an unlisted product
    has none — and the rehearsal will not pick a near-match on the buyer's behalf."""
    fork = proving_fake("teststore", fresh_pubkey(), "Water", 100_000)
    out = as_account(
        {"store": "teststore", "product": "Coffee", "rpc_url": fork.rpc_url},
        rpc_call=fork,
    )
    assert out["code"] == "product-unknown"
    assert "Water" in out["reason"]
    assert "sendTransaction" not in {method for _url, method in fork.seen}


# ---------------------------------------------------------------------------
# the fork leg: the tool, against a validator, buying something for real
# ---------------------------------------------------------------------------


@needs_fork
@pytest.mark.fork
def test_the_tool_lands_a_real_purchase_on_a_real_fork() -> None:
    """End to end through the MCP entry point — not the library function underneath it.

    This is the probe that "wired" cannot substitute for: it starts a fork, calls the tool
    the way a client would, and asserts the ledger moved by exactly the store's own price.
    """
    from tests.test_sandbox_rehearse import local_store

    config = local_store()
    product = os.environ.get("GECKO_CI_PRODUCT", "Water")
    mainnet = os.environ.get("GECKO_MAINNET_RPC", "https://api.mainnet-beta.solana.com")

    with SurfpoolFork(mainnet, port=FORK_PORT, ready_timeout=180) as fork:
        out = OrquestraCatalogSurface().call_tool(
            "try_purchase",
            {
                "store": config["store"],
                "product": product,
                "table": 7,
                "rpc_url": fork.rpc_url,
            },
        )

    assert out["sandbox"] is True
    assert out["refused"] is False, out.get("reason")
    assert out["landed"] is True
    assert out["discrepancies"] == []
    price = out["price_raw"]
    assert out["moved"]["buyer_token"]["moved"] == -price
    assert out["moved"]["store_token"]["moved"] == price
    assert out["receipt"]["price_raw"] == price
    assert out["reset"], "the fork was left holding this run's state"
    # the caveat rides along even on the run that worked
    assert out["what_this_does_not_prove"]


# ---------------------------------------------------------------------------
# Finding 2 from the adversarial pass: this tool is mounted on a PUBLIC,
# UNAUTHENTICATED surface, so its url handling is somebody else's attack surface.
# ---------------------------------------------------------------------------


def test_a_hostname_is_refused_without_ever_resolving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DNS rebinding window, closed by removing DNS rather than pinning it.

    The guard used to resolve the name and require every answer to be loopback. The
    transport then resolved it AGAIN — two independent `getaddrinfo` calls per
    invocation, nothing pinned — so a name answering 127.0.0.1 here and something else
    there turned this process into an authenticated proxy, and into a signer for
    somebody else's chain if the rebound host ran a surfnet.

    This asserts the count, not the outcome: a refusal that still resolved would pass an
    outcome check while leaving the window open.
    """
    import socket

    calls: list[str] = []
    real = socket.getaddrinfo

    def counting(host: object, *rest: object, **kw: object) -> object:
        calls.append(str(host))
        return real(host, *rest, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", counting)

    out = as_account(
        {"rpc_url": "http://fork.attacker.example:8899", "store": "s", "product": "p"}
    )
    assert out["code"] == "not-a-local-fork"
    assert out["sandbox"] is True
    assert calls == [], f"the name was resolved {len(calls)}x — that is the window"


def test_a_refusal_reports_the_error_CLASS_and_never_the_target_text() -> None:
    """The echo, closed. On a hosted deployment loopback is OUR machine.

    An adversarial pass pointed this at a service that answers with a banner and read
    `INTERNAL-SERVICE-BANNER-s3cr3t-token-abc123` straight out of the refusal reason. A
    stranger echoing arbitrary local error text is a read primitive, not a diagnostic.
    """

    def banner(url: str, method: str, params: object) -> dict[str, object]:
        raise RpcError("INTERNAL-SERVICE-BANNER-s3cr3t-token-abc123")

    out = as_account(
        {"rpc_url": "http://127.0.0.1:1", "store": "s", "product": "p"},
        rpc_call=banner,
    )
    assert out["code"] == "no-fork-proved"
    assert "s3cr3t" not in out["reason"]
    assert "BANNER" not in out["reason"]
    # the class is still there, because a refusal an agent cannot act on is a shrug
    assert "Error" in out["reason"] or "error" in out["reason"]


def test_localhost_is_rewritten_and_not_looked_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`localhost` is the one name an agent will type and the one answer we already know.

    Substituting the literal keeps DNS out of the path rather than trusting it to say
    what it is supposed to — a hosts file, a search domain or a resolver cache can each
    make `localhost` mean something else.
    """
    import socket

    calls: list[str] = []
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, *a, **k: calls.append(str(host)) or []
    )
    assert _pin_localhost("http://localhost:8899") == "http://127.0.0.1:8899"
    assert _pin_localhost("http://LocalHost:8899") == "http://127.0.0.1:8899"
    assert _pin_localhost("http://127.0.0.1:8899") == "http://127.0.0.1:8899"
    assert calls == []


def test_an_anonymous_caller_gets_no_rehearsal_and_no_key() -> None:
    """The residual the adversarial pass left standing: this tool was mounted openly.

    The gate is NOT about custody — a rehearsal spends nothing of anyone's and its buyer
    is born and discarded inside the call. It is about what an anonymous caller can make
    this process DO: issue outbound JSON-RPC from our machine and burn a fork's compute.
    With names refused and error text reduced to a class the residue is only a weak
    "does something answer on this loopback port" oracle — and a stranger needs neither.

    The refusal has to land before ANY argument handling, or a caller learns which
    arguments are wrong by asking.
    """
    out = try_purchase_result({"rpc_url": "http://127.0.0.1:8899", "store": "s"})
    assert out["refused"] is True
    assert out["code"] == "account-required"
    assert out["sandbox"] is True
    # nothing about the arguments is disclosed: no missing-`product` complaint
    assert "product" not in out["reason"]


def test_an_authenticated_caller_reaches_the_normal_refusals() -> None:
    """A gate that also swallows the real answers is a gate nobody can debug through."""
    out = try_purchase_result(
        {
            "rpc_url": "https://api.mainnet-beta.solana.com",
            "store": "s",
            "product": "p",
        },
        account="acct_1",
    )
    assert out["code"] == "not-a-local-fork"
