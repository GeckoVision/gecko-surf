"""The tool-result cap must keep VALID JSON for large list payloads.

`getNews` returns ~57KB (each article carries a huge Google-RSS URL). Byte-truncating
to max_chars cut the JSON mid-string → the model received invalid JSON and the /noticias
command answered unreliably. The cap must trim the list structurally instead.
"""

from __future__ import annotations

import json


def test_large_list_payload_stays_valid_json_within_cap() -> None:
    from examples.sos_vzla_bot.surfcall_tools import _cap

    big = {
        "status": 200,
        "data": [
            {"title": "noticia " * 8, "url": "https://news.google.com/" + "x" * 200}
            for _ in range(80)
        ],
    }
    out = _cap(big, max_chars=2000)

    assert len(out) <= 2000
    parsed = json.loads(out)  # MUST parse — not byte-truncated mid-string
    assert parsed["truncated"] is True
    assert 1 <= len(parsed["data"]) < 80  # kept the most-recent items, dropped the rest


def test_small_payload_passes_through_unchanged() -> None:
    from examples.sos_vzla_bot.surfcall_tools import _cap

    small = {"status": 200, "data": [{"a": 1}, {"b": 2}]}
    out = _cap(small, max_chars=6000)
    assert json.loads(out) == small  # untouched, no 'truncated' flag
