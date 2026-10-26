"""Bearer-token check shared by BearerAuthMiddleware and the /health detail gate.

The auth scheme name is case-insensitive (RFC 7235 §2.1), so "bearer" and
"BEARER" are accepted. The token itself is compared in constant time.

Also holds the one-segment rule for tokenless paths, shared by the middleware
and the code that mints those URLs (reminder callback links).
"""

from __future__ import annotations

import hmac
import re

# A path segment a tokenless caller may put after a public prefix: the audio
# filename or reminder id. Excludes "/", "\", "%", "?", "#", whitespace,
# control characters and non-ASCII, so decoded or double-encoded traversal
# can't survive. Reminder ids (uuid hex, rec_<rule>_<ts>, mp-/pc-<hex>) and
# audio names (<uuid8>.<ext>) fit; an id format outside it would 401 its taps.
_PUBLIC_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")


def bearer_token_matches(authorization: str, expected_token: str) -> bool:
    """True iff ``authorization`` is ``<Bearer scheme> <expected_token>``.

    An empty ``expected_token`` never matches, so an unset token can't be
    satisfied by a bare "Bearer " header.
    """
    if not expected_token:
        return False
    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(token.strip().encode("utf-8"), expected_token.encode("utf-8"))


def is_public_segment(segment: str) -> bool:
    """True iff ``segment`` is safe to exempt from the bearer check."""
    return segment not in (".", "..") and _PUBLIC_SEGMENT_RE.fullmatch(segment) is not None
