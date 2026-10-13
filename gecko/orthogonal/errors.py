"""Typed failures for the commenter-list job, each one a sentence a person can act on.

Every message names the cause and the next step. Every message is passed through
``redact`` on construction, so no path can raise with a key inside it — even if a
future caller formats an upstream body into one.
"""

from __future__ import annotations

import re

# An Orthogonal key, an Authorization value, or a bare bearer token. Over-matching is
# fine here: a redacted request id costs nothing, a leaked key costs the balance.
_SECRET_PATTERNS = (
    re.compile(r"orth_(?:live|test)_[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"(?i)authorization\s*[:=]\s*\S+"),
)


def redact(text: str) -> str:
    """``text`` with anything key-shaped replaced by ``[redacted]``."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


class ListJobError(Exception):
    """A refusal or failure of the commenter-list job. ``code`` is stable for callers.

    ``charge_possible`` says whether the upstream may have charged for the call that
    failed: False for every refusal made before a request left (and for an answer that
    says nothing was spent), True once a paid request was sent and its reply was not a
    success. The spend guard keeps such a call's hold at its quote instead of releasing
    it (founder-approved, 9 Oct); ``counted_cents`` is what it counted.
    """

    code = "list_job_error"
    charge_possible = False

    def __init__(self, message: str) -> None:
        self.message = redact(message)
        self.counted_cents = 0
        super().__init__(self.message)


class NoKeyError(ListJobError):
    code = "no_key"

    def __init__(self) -> None:
        super().__init__(
            "Gecko has no Orthogonal key on this machine. "
            "Run `gecko auth set orthogonal --scheme bearer`."
        )


class OutOfCreditsError(ListJobError):
    code = "out_of_credits"

    def __init__(self, balance: str, needed_cents: int) -> None:
        super().__init__(
            f"Orthogonal credits: {balance} left, this list needs "
            f"${needed_cents / 100:.2f}. Nothing was spent. Top up at the dashboard."
        )


class SheetOutsideFolderError(ListJobError):
    code = "sheet_outside_folder"

    def __init__(self, folder: str, name: str) -> None:
        super().__init__(f"I can only read files in {folder}. Put {name} there.")


class SheetError(ListJobError):
    """The sheet is in the folder but cannot be used (missing, not CSV, too big)."""

    code = "sheet_unreadable"


class NotAllowedError(ListJobError):
    """A call outside the hard allowlist. Refused before any request is built."""

    code = "not_allowed"


class SpendCapError(ListJobError):
    code = "spend_cap"


class ContinuationChargedError(ListJobError):
    """A follow-up page charged money. Pages after the first are documented as free."""

    code = "continuation_charged"


class BadPostUrlError(ListJobError):
    code = "bad_post_url"

    def __init__(self) -> None:
        super().__init__(
            "That is not an Instagram post or reel link. "
            "Paste a link like https://www.instagram.com/p/<code>/."
        )


class NotConfirmedError(ListJobError):
    code = "not_confirmed"


class UpstreamError(ListJobError):
    """Orthogonal answered with a failure, or the wire broke after the request was sent.
    Carries the status, never the body. The call may have been charged."""

    code = "upstream_error"
    charge_possible = True

    def __init__(
        self,
        message: str,
        status: int | None = None,
        *,
        charge_possible: bool | None = None,
    ) -> None:
        self.status = status
        if charge_possible is not None:
            # An answer to a request that carried no payment cannot have charged.
            self.charge_possible = charge_possible
        super().__init__(message)


class KeyRejectedError(UpstreamError):
    """401/403: every further call would be refused the same way, so a run stops.
    Orthogonal refused the key, so nothing was spent."""

    code = "key_rejected"
    charge_possible = False


class X402RefusedError(ListJobError):
    """An x402 payment refused BEFORE anything was signed or sent: the challenge did not
    match what Gecko pinned, or the pre-sign gate or the signer said no. Nothing paid."""

    code = "x402_refused"


def charge_possible(exc: BaseException) -> bool:
    """True when ``exc`` failed a call that may already have been charged upstream.
    Anything that is not one of ours is read as raised before the request left: the
    transport's own failures reach callers as :class:`UpstreamError`."""
    return isinstance(exc, ListJobError) and exc.charge_possible


class ParquetUnavailableError(ListJobError):
    code = "parquet_unavailable"

    def __init__(self) -> None:
        super().__init__(
            "Saving as Parquet needs pyarrow, which is not installed. "
            "Run `uv sync --extra datasets` (or `pip install 'gecko-surf[datasets]'`)."
        )


# The two notices the job reports without failing: the run still produced a list.
NO_HANDLE_COLUMN_NOTICE = (
    "Your sheet has no Instagram column, so every name is marked 'not sure'. "
    "Add a column named `instagram`."
)


def zero_comments_notice(charged_cents: int) -> str:
    return (
        "This post returned 0 comments. It may be private. "
        f"You were charged ${charged_cents / 100:.2f}."
    )
