# --- a bare number is a quantity, not identity — but only in PROGRAM routing ----------


def test_digit_only_tokens_survive_the_shared_layer() -> None:
    """`content_tokens` keeps digits ON PURPOSE: in API-operation search a bare number
    can be the selector itself — "kind 77" finds `getWidgetKind77` by the 77. The
    quantity-vs-identity call is only decidable in program routing, so the drop lives in
    `find_start._query_tokens`, not here. The first draft put it here and broke exactly
    that scale-projection case."""
    from gecko.lexnorm import content_tokens

    assert "77" in content_tokens({"77", "widget"})
