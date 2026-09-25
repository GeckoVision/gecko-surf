"""Engine answers, rendered for a person in a chat window.

The surface returns a nested dict built for an agent. A person gets one message with
a hard 4096-character ceiling, no folding and no scrollback they will read. So this
module chooses what survives, and the choices are the interesting part:

* A REFUSAL and an ERROR are rendered differently on purpose. A refusal is a correct
  answer — the store does not exist, the plan pays the buyer back, the simulation
  reverted — and the reader should act on it. An error means Gecko broke, and the
  reader should retry. Collapsing them into "sorry, something went wrong" is the
  single most common way a working refusal gets read as a bug.
* A prepared purchase leads with WHAT WOULD MOVE and who signs, because Gecko holds
  no key: the honest end state is bytes plus a statement about them.
* The unsigned transaction is included only when it FITS. Truncated base64 is worse
  than absent base64 — it looks signable and is not — so a message that cannot carry
  it says so and names where to get it.

Pure formatting: dicts in, text out. No I/O, nothing stored.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "CANNOT_ANSWER",
    "MAX_MESSAGE_CHARS",
    "NEED_BUYER",
    "NEED_PRODUCT",
    "NEED_STORE",
    "browse_text",
    "clip",
    "error_text",
    "help_text",
    "purchase_text",
]

#: Telegram's per-message ceiling. Anything longer is rejected by the API, so this is
#: a wire limit and not a style preference.
MAX_MESSAGE_CHARS = 4096

#: How many stores/products one message shows before it says there are more. A menu
#: that fills the window buries the next step (how to buy) under the thing it answers.
_MAX_STORES = 8
_MAX_PRODUCTS = 6

#: Reserved for the tail line after the base64 bytes, so the inclusion test cannot
#: pass and then overflow on the sentence that follows.
_TX_TAIL_SLACK = 320


def clip(text: str) -> str:
    """Fit ``text`` into one Telegram message, marking it when the tail was dropped.

    The marker is load-bearing: a silently truncated answer reads as a complete one,
    and this module's whole job is to not let a reader believe something it should not.
    """
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    marker = "\n… (cut short)"
    return text[: MAX_MESSAGE_CHARS - len(marker)] + marker


def help_text() -> str:
    """What this chat can do, in the words that work. Every line is a shape the
    parser accepts, so the help doubles as the grammar."""
    return (
        "I read Solana storefronts and prepare purchases you sign yourself.\n"
        "\n"
        "Try:\n"
        "• stores — every storefront and its prices\n"
        "• menu for geckocoffee — one store\n"
        "• how much is espresso\n"
        "• buy espresso from geckocoffee for <your wallet address>\n"
        "\n"
        "I never hold a key and never sign. A purchase comes back as unsigned "
        "bytes plus what they would move; you sign them in your own wallet.\n"
        "Prices are read from the chain, not from a list I keep."
    )


#: What a buy is missing, said in the shape that works. A refusal a reader cannot act
#: on costs them another turn, which on this path is also a blockhash window.
NEED_STORE = (
    "I need the store as well as the product — a product name alone is not enough to "
    "find the right storefront.\n"
    "Say: buy <product> from <store> for <your wallet address>.\n"
    "Send `stores` first if you want to see what exists."
)
NEED_PRODUCT = (
    "I need to know what to buy.\n"
    "Say: buy <product> from <store> for <your wallet address>."
)
NEED_BUYER = (
    "I need your wallet address, and I need it from you: Gecko holds no key and has "
    "no wallet of yours to look up.\n"
    "Say: buy <product> from <store> for <your wallet address>.\n"
    "The address is only used to build the unsigned transaction — you sign it yourself."
)
CANNOT_ANSWER = (
    "I could not read that as something I do. This is a refusal, not a failure — I "
    "only do three things:\n"
    "• stores — every storefront and its prices\n"
    "• how much is <product>\n"
    "• buy <product> from <store> for <your wallet address>\n"
    "Send /help for the long version."
)


def error_text(kind: str) -> str:
    """Gecko broke. Named as OUR failure, never dressed as a refusal, and carrying
    only the exception class — an error body is untrusted transport output."""
    return (
        f"Something failed on my side ({kind}). This is a bug or a flaky node, "
        "not a refusal — try again in a moment. If it keeps happening the store "
        "data is unreadable right now and nothing should be signed."
    )


def _refusal_text(result: Mapping[str, Any]) -> str:
    """A structured refusal, rendered as the answer it is."""
    code = str(result.get("code") or "refused")
    reason = str(result.get("reason") or "no reason given")
    return f"No — {code}.\n{reason}"


def _engine_error_text(result: Mapping[str, Any]) -> str:
    """The surface's own ``{"error": ...}`` shape, which is a transport failure and so
    reads as an error rather than a refusal. ``error`` is sometimes a string and
    sometimes a dict of code/message/hint; both are rendered without inventing fields."""
    error = result.get("error")
    if isinstance(error, Mapping):
        parts = [str(error.get("message") or error.get("code") or "call failed")]
        hint = error.get("hint")
        if hint:
            parts.append(str(hint))
        return "Something failed on my side.\n" + "\n".join(parts)
    return f"Something failed on my side.\n{error}"


def _price_line(product: Mapping[str, Any]) -> str:
    """One menu line. The mint is named because two mints can wear the same label and
    a wallet holding one cannot pay where the other is priced."""
    name = str(product.get("name") or "unnamed")
    price = product.get("price_ui")
    mint = str(product.get("mint") or "")
    label = product.get("mint_note") or (mint[:4] + "…" + mint[-4:] if mint else "?")
    return f"  • {name} — {price} {label}"


def browse_text(result: Mapping[str, Any], *, filtered: bool) -> str:
    """A menu, or the honest empty answer.

    ``filtered`` changes the empty case and only the empty case: with no filter,
    nothing found means nothing is there; with one, it means the filter matched
    nothing — two different facts that the same sentence would blur.
    """
    if "error" in result:
        return _engine_error_text(result)
    stores = result.get("stores")
    if not isinstance(stores, list) or not stores:
        if filtered:
            return (
                "Nothing matched that. Send `stores` for everything on offer — the "
                "filter is a plain substring, so a near-miss spelling finds nothing."
            )
        return (
            "No storefronts on mainnet right now. Nothing is hidden from you; the "
            "program simply holds no readable store account."
        )

    lines: list[str] = []
    for store in stores[:_MAX_STORES]:
        if not isinstance(store, Mapping):
            continue
        name = str(store.get("store") or "unnamed store")
        # Plain text, no Markdown: the sender posts without `parse_mode`, so a store
        # name carrying an asterisk or underscore cannot break the message it is in.
        lines.append(f"{name}:")
        products = store.get("products")
        if isinstance(products, list) and products:
            lines += [
                _price_line(product)
                for product in products[:_MAX_PRODUCTS]
                if isinstance(product, Mapping)
            ]
            if len(products) > _MAX_PRODUCTS:
                lines.append(f"  … {len(products) - _MAX_PRODUCTS} more products")
        else:
            lines.append("  (no products listed)")
        fulfilment = store.get("fulfilment")
        if isinstance(fulfilment, Mapping) and not fulfilment.get("set"):
            # Said before anyone pays, not after: the program records the purchase and
            # tells nobody to make it. That is a fact about the store, not an error.
            lines.append(
                "  ⚠ no delivery channel set — a purchase here is recorded "
                "on chain and nobody is told to fulfil it"
            )
        lines.append("")

    if len(stores) > _MAX_STORES:
        lines.append(
            f"… {len(stores) - _MAX_STORES} more stores. Name one to narrow it."
        )
    skipped = result.get("skipped_undecodable")
    if isinstance(skipped, int) and skipped:
        lines.append(
            f"({skipped} program accounts were not readable as a store — counted, "
            "not guessed at.)"
        )
    lines.append("To buy: buy <product> from <store> for <your wallet address>.")
    return clip("\n".join(lines).strip())


def _effects_lines(effects: Mapping[str, Any]) -> list[str]:
    """What the simulation OBSERVED moving. Absent facts stay absent: a line we did
    not measure must not appear as a measured zero."""
    lines: list[str] = []
    tokens = effects.get("tokens_out")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, Mapping):
                continue
            mint = str(token.get("mint") or "")
            short = mint[:4] + "…" + mint[-4:] if len(mint) > 8 else mint
            lines.append(f"  − {token.get('amount')} of {short} leaves your wallet")
    sol = effects.get("sol_delta")
    if isinstance(sol, Mapping) and sol.get("sol") is not None:
        lines.append(f"  − {sol.get('sol')} SOL (network fee and rent)")
    unmeasured = effects.get("unmeasured")
    if isinstance(unmeasured, list) and unmeasured:
        lines.append(f"  ? not measured: {', '.join(str(x) for x in unmeasured)}")
    return lines


def purchase_text(result: Mapping[str, Any]) -> str:
    """A prepared purchase: what moves, who signs, when it expires, then the bytes.

    A refusal and a transport error come back through here too, because the caller
    cannot tell them apart from the outside and this module already knows how each is
    shaped. Every one of the three renders as a different kind of sentence.
    """
    if result.get("refused"):
        return clip(_refusal_text(result))
    if "error" in result:
        return _engine_error_text(result)

    lines = ["Prepared. Nothing is signed and nothing has moved yet.", ""]

    effects = result.get("effects")
    if isinstance(effects, Mapping):
        moved = _effects_lines(effects)
        if moved:
            lines.append("What these bytes would move:")
            lines += moved
            lines.append("")
    else:
        # The message could not be decoded, so any summary would be a guess wearing a
        # decoder's authority. Say that rather than write the sentence.
        lines += [
            "I could not decode these bytes into a summary, so I am not going to "
            "describe what they move. Do not sign them on my word.",
            "",
        ]

    transaction = result.get("transaction")
    who_signs = (
        transaction.get("who_signs") if isinstance(transaction, Mapping) else None
    )
    if who_signs:
        lines.append(f"Who signs: {who_signs}")
    expires = result.get("expires")
    if isinstance(expires, Mapping):
        # A BUDGET, not a sentence: a blockhash dies at a block height, and "about 40
        # seconds" is not something a reader can subtract from while they fetch a wallet.
        seconds = expires.get("seconds_remaining_estimate")
        blocks = expires.get("blocks_remaining")
        if isinstance(seconds, int) and isinstance(blocks, int):
            lines.append(
                f"Expires in about {seconds}s ({blocks} blocks). Past that these "
                "bytes will not land and you ask me again — which is free."
            )
        else:
            lines.append(
                "Expires with its blockhash, and I could not read how much of that "
                "is left. Ask me again rather than signing a stale one."
            )
    submit = result.get("submit")
    if isinstance(submit, Mapping) and submit.get("rpc_url"):
        lines.append(f"Send to: {submit.get('rpc_url')}")
    lines.append("")

    head = "\n".join(lines)
    raw = (
        transaction.get("unsigned_transaction")
        if isinstance(transaction, Mapping)
        else None
    )
    if isinstance(raw, str) and raw:
        if len(head) + len(raw) + _TX_TAIL_SLACK <= MAX_MESSAGE_CHARS:
            return clip(
                head
                + "Unsigned transaction (base64):\n"
                + raw
                + "\n\nSign these exact bytes in your own wallet. I hold no key."
            )
        # Truncated base64 looks signable and is not. Withhold it and say where it is.
        return clip(
            head + "The unsigned bytes are too long for one chat message, so I am not "
            "pasting a partial copy of something you would sign. Call "
            "`prepare_purchase` on the MCP surface for the same bytes."
        )
    return clip(
        head + "No transaction came back, so there is nothing to sign. Treat that as "
        "a failure on my side, not as a purchase."
    )
