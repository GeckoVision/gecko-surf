"""Accept only an Instagram post or reel link, and name it by its shortcode.

The URL is passed upstream as a query value, so it is checked here rather than trusted:
https only, instagram.com only, ``/p/``, ``/reel/`` or ``/reels/`` plus a shortcode.
The shortcode is the post id the stored list and the receipt carry.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .errors import BadPostUrlError

_HOSTS = {"instagram.com", "www.instagram.com"}
_PATH = re.compile(r"^/(?:p|reel|reels)/([A-Za-z0-9_-]{5,64})/?$")


def post_id_from_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 300:
        raise BadPostUrlError()
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in _HOSTS:
        raise BadPostUrlError()
    if parts.username or parts.password or parts.port:
        raise BadPostUrlError()
    match = _PATH.match(parts.path)
    if match is None:
        raise BadPostUrlError()
    return match.group(1)


def canonical_post_url(url: str) -> str:
    """The URL sent upstream: same post, no query string or fragment (tracking params)."""
    parts = urlsplit(url.strip())
    post_id_from_url(url)
    return f"https://www.instagram.com{parts.path.rstrip('/')}/"
