"""A Jupyter notebook, projected onto markdown so a document corpus can rank it.

WHY THIS IS A PROJECTOR AND NOT A SUFFIX. ``doccorpus.load_pages`` reads text files
and splits them into lines. A notebook is JSON, so adding ``.ipynb`` to ``suffixes``
would index the serialisation: cell ids, metadata, execution counts and base64
images, none of which a student ever reads. The projector is the thing that decides
what a notebook MEANS as prose, and that decision belongs in one place.

WHAT IS KEPT, AND WHAT IS DROPPED ON PURPOSE:

* **markdown cells** — kept verbatim. This is the teaching.
* **code cells** — kept as fenced source. The exercises live here, and a student
  asking "how do I write ``redact``" is asking about a code cell.
* **outputs** — DROPPED, always. An output is the result of somebody's run: it can
  hold a name, a key a learner pasted, an API response, a rendered image. None of
  it is course content, and a control-plane corpus has no business storing it.
  This is the same rule that keeps response payloads out of the engine.
* **the Colab badge cell** — dropped. It is navigation furniture that every
  notebook shares, so indexing it makes 66 documents look alike to a scorer.

Nothing here takes a ``text=`` argument, for the reason ``doccorpus`` states: a
builder that accepts text is a builder an agent can feed.
"""

from __future__ import annotations

import json
from pathlib import Path

#: Cells whose source starts with one of these are furniture, not content. Every
#: notebook in a course carries them, so indexing them teaches the scorer nothing
#: and makes unrelated notebooks look similar.
FURNITURE = ("<!-- colab-badge -->", "# @title", "# manual-run:")


class NotebookError(Exception):
    """A file that does not parse as a notebook. Never raised for an empty one."""


def _cell_text(cell: dict) -> str | None:
    """One cell as markdown, or None when it contributes nothing."""
    source = "".join(cell.get("source") or []).strip()
    if not source:
        return None
    kind = cell.get("cell_type")
    if kind == "markdown":
        return source if not source.startswith(FURNITURE) else None
    if kind == "code":
        if source.startswith(FURNITURE):
            return None
        # Fenced, so the chunker's paragraph packing does not split a function in
        # half and hand back a body with no `def` line above it.
        return f"```python\n{source}\n```"
    # Raw cells are usually nbconvert directives. Nothing a learner reads.
    return None


def notebook_markdown(path: Path) -> str:
    """The notebook's cells in order, as markdown. Outputs never included.

    Returns an empty string for a notebook with no readable cells, which the loader
    treats the same way it treats an empty page: skipped, not an error.
    """
    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NotebookError(
            f"{path.name} does not parse as a notebook: {exc}"
        ) from None
    if not isinstance(notebook, dict):
        raise NotebookError(f"{path.name} is not a notebook object")
    parts = [
        text
        for cell in notebook.get("cells") or []
        if isinstance(cell, dict) and (text := _cell_text(cell)) is not None
    ]
    return "\n\n".join(parts)
