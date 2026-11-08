"""Optional response validation through the published ``gba_contracts`` models."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType
from typing import Any

from pydantic import BaseModel, ValidationError

from gba_agent.clients import peer_contracts_compat as local_models

shared_models: ModuleType | None
try:
    shared_models = import_module("gba_contracts.models")
except ModuleNotFoundError as exc:
    if exc.name not in {"gba_contracts", "gba_contracts.models"}:
        raise
    shared_models = None


class UpstreamContractError(ValueError):
    """A peer response violates an available shared Pydantic contract."""


def validate_shared_model(model_name: str, payload: Any) -> dict[str, Any]:
    """Shared-first validation, with explicit frozen local DTOs when absent."""
    module = shared_models if shared_models is not None else local_models
    model = getattr(module, model_name, None)
    if model is None:
        raise RuntimeError(f"gba_contracts.models.{model_name} is not available")
    try:
        validated = model.model_validate(payload)
    except ValidationError as exc:
        raise UpstreamContractError(
            f"Peer response did not match gba_contracts.models.{model_name}"
        ) from exc
    normalized = validated.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if not isinstance(normalized, dict):
        raise UpstreamContractError(
            f"gba_contracts.models.{model_name} did not serialize to an object"
        )
    return normalized


def validate_request(model_name: str, payload: Any) -> dict[str, Any]:
    module = shared_models if shared_models is not None else local_models
    model: type[BaseModel] = getattr(module, model_name)
    try:
        return model.model_validate(payload).model_dump(mode="json", exclude_unset=True)
    except ValidationError:
        raise ValueError(f"Request does not match frozen {model_name}") from None


def validate_shared_items(model_name: str, payload: Any, *, field: str = "items") -> dict[str, Any]:
    """Validate each model in a contract response list."""
    if (
        not isinstance(payload, dict)
        or set(payload) != {field}
        or not isinstance(payload.get(field), list)
    ):
        raise UpstreamContractError(f"Peer response field {field!r} must be a list")
    return {
        **payload,
        field: [validate_shared_model(model_name, item) for item in payload[field]],
    }


def validate_receipt(payload: dict[str, Any], fields: dict[str, type]) -> dict[str, Any]:
    """Validate unnamed response objects from the frozen endpoint tables."""
    if set(payload) != set(fields) or any(
        type(payload[name]) is not kind or payload[name] == "" for name, kind in fields.items()
    ):
        raise UpstreamContractError("Peer returned an invalid receipt")
    return payload
