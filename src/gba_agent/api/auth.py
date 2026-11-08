"""Shared bearer-token authentication."""

from __future__ import annotations

import hmac

from gba_agent.api.errors import make_error
from gba_agent.config import Settings


def authenticate(
    authorization: str | None, settings: Settings, request_id: str | None = None
) -> dict[str, object] | None:
    """Return a shared 401 envelope unless the bearer token matches."""
    if authorization is None:
        return make_error(
            code="UNAUTHORIZED",
            message="Missing or invalid bearer token",
            request_id=request_id,
        )

    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or not hmac.compare_digest(token, settings.service_token.get_secret_value())
    ):
        return make_error(
            code="UNAUTHORIZED",
            message="Missing or invalid bearer token",
            request_id=request_id,
        )
    return None
