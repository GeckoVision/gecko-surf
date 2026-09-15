"""The store layout, pinned against a copy of it that lives in another repository.

The Dev3Pack course ships an optional track in which a learner builds a
`let_me_buy` storefront. The course declares exactly one runtime dependency and
CI installs it frozen, so it cannot import this package; it carries a
hand-written stdlib mirror of `encode_store`/`decode_store` instead.

A mirror is only safe if drift is loud on BOTH sides. The course asserts its copy
reproduces these bytes. This asserts ours still does. Change the layout here
without telling anyone and this test goes red today, rather than a cohort's
offline checks going quietly wrong months from now.

The constant is the same base64 string the course keeps at
`ship-it/fixtures/geckocoffee-store.b64`. If you are updating the layout on
purpose, update both.
"""

from __future__ import annotations

import base64

from gecko.store_directory import StoreListing, StoreProduct, decode_store, encode_store

#: Two products, a channel, and seven purchases. Real in shape, fictional in
#: content: the authority is a public address and nobody has bought anything.
GOLDEN_B64 = (
    "3vXtQDsxHfYAAAAABwAAAAAAAAALAAAAZ2Vja29jb2ZmZWVTm6HFMWD6ZGnEgxe7GRls6v2d2uADRlIu"
    "61c/bFRSDgIAAADoAwAAAAAAAAbG+nrzvtutOj1l82qryXQxsbvkwtL24OR8pgIDRS9dYQgAAABFc3By"
    "ZXNzb9wFAAAAAAAABsb6evO+2606PWXzaqvJdDGxu+TC0vbg5HymAgNFL11hCgAAAENhcHB1Y2Npbm8T"
    "AAAAQGdlY2tvY29mZmVlLW9yZGVycw=="
)

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

LISTING = StoreListing(
    store_name="geckocoffee",
    address="11111111111111111111111111111111",
    authority="6dNUsdmH2dzXe2k5Vy1nDXAiVvwjvhCbGnCFpkSgfPCy",
    total_purchases=7,
    products=(
        StoreProduct(name="Espresso", price_raw=1000, decimals=6, mint=USDC),
        StoreProduct(name="Cappuccino", price_raw=1500, decimals=6, mint=USDC),
    ),
    telegram_channel_id="@geckocoffee-orders",
)


def test_the_layout_the_course_mirrors_has_not_moved() -> None:
    assert encode_store(LISTING) == base64.b64decode(GOLDEN_B64)


def test_those_bytes_still_decode_to_what_the_course_expects() -> None:
    """`address` is not in the bytes -- it is where they were read from."""
    decoded = decode_store(base64.b64decode(GOLDEN_B64), address=LISTING.address)
    assert decoded == LISTING
