"""Bearer-token check shared by BearerAuthMiddleware and the /health detail gate.

The auth scheme name is case-insensitive (RFC 7235 §2.1), so "bearer" and
"BEARER" are accepted. The token itself is compared in constant time.
"""

from __future__ import annotations

import hmac


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
