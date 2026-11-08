"""Request tracing context and shared identifier generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ulid import ULID

ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def new_ulid() -> str:
    """Return a new canonical ULID string."""
    return str(ULID())


def is_ulid(value: str) -> bool:
    """Check that a value is a canonical, uppercase ULID."""
    return ULID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class TraceContext:
    """Tracing headers that must follow a downstream service call."""

    request_id: str
    run_id: str | None
    matter_id: str | None
    contract_version: str

    def as_headers(self) -> dict[str, str]:
        """Build the supported tracing-header set."""
        headers = {
            "X-Request-Id": self.request_id,
            "X-Contract-Version": self.contract_version,
        }
        if self.run_id is not None:
            headers["X-Run-Id"] = self.run_id
        if self.matter_id is not None:
            headers["X-Matter-Id"] = self.matter_id
        return headers


def trace_from_request(request: Any) -> TraceContext:
    """Read the middleware-created trace context from a FastAPI request."""
    trace = getattr(request.state, "trace", None)
    if not isinstance(trace, TraceContext):
        raise RuntimeError("request tracing middleware did not install TraceContext")
    return trace
