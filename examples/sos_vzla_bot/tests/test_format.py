"""Telegram message hardening — split replies to the 4096-char limit. Pure, offline."""

from __future__ import annotations

from examples.sos_vzla_bot.bot import chunk_message


def test_short_text_is_one_chunk():
    assert chunk_message("hola", 4096) == ["hola"]


def test_splits_on_paragraph_boundary():
    text = "a" * 3000 + "\n\n" + "b" * 3000
    chunks = chunk_message(text, 4096)
    assert len(chunks) == 2
    assert all(len(c) <= 4096 for c in chunks)
    assert chunks[0] == "a" * 3000 and chunks[1] == "b" * 3000


def test_hard_split_when_no_boundary_preserves_content():
    text = "x" * 5000
    chunks = chunk_message(text, 4096)
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


def test_no_empty_chunks_with_many_newlines():
    text = "a" * 2000 + "\n\n\n\n" + "b" * 3000
    chunks = chunk_message(text, 4096)
    assert "" not in chunks
    assert all(len(c) <= 4096 for c in chunks)
