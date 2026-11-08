"""Validated Track A and shared service configuration."""

from __future__ import annotations

import base64
import binascii
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    """Application settings loaded from environment variables or a local .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
        hide_input_in_errors=True,
    )

    gba_env: Literal["dev", "test", "prod"] = "dev"
    service_token: SecretStr
    database_url: SecretStr
    agent_api_url: str = "http://localhost:8100"
    graph_api_url: str = "http://localhost:8101"
    workbench_api_url: str = "http://localhost:8102"
    peer_mode: Literal["live", "mock", "replay"] = "live"
    graph_mock_api_url: str = "http://localhost:18101"
    workbench_mock_api_url: str = "http://localhost:18102"
    peer_replay_dir: Path = Path("./.peer_replay")
    blob_url: str = "file://./.blobs"
    blob_s3_key: SecretStr | None = None
    blob_s3_secret: SecretStr | None = None
    llm_provider: Literal["anthropic", "openai_compatible"]
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model_planner: str
    llm_model_extractor: str | None = None
    llm_model_judge: str | None = None
    llm_model_vlm: str | None = None
    llm_model_drafter: str | None = None
    llm_cache_dir: Path = Path("./.llm_cache")
    llm_mode: Literal["live", "record", "replay"] = "live"
    traj_dir: Path = Path("./trajectory")
    registry_path: Path = Path("./config/registry.yaml")
    agent_user_agent: str
    as_of_leakage_mode: Literal["strict", "off"] = "strict"
    gba_ca_bundle: Path | None = None
    courtlistener_token: SecretStr | None = None

    session_dir: Path = Path("./.sessions")
    session_enc_key: SecretStr | None = None
    browser_mode: Literal["headless", "headed"] = "headless"
    novnc_base_url: str = "http://localhost:6080"
    handoff_timeout_seconds: int = Field(default=900, gt=0)
    vault_backend: Literal["keyring", "file", "none"] = "none"
    vault_file: Path | None = None
    vault_file_key: SecretStr | None = None
    support_judge: Literal["llm", "nli", "both"] = "llm"
    support_min_score: float = Field(default=0.7, ge=0, le=1)
    nli_model: str = "cross-encoder/nli-deberta-v3-base"
    max_gnn_loops: int = Field(default=2, ge=0)
    abstain_min_confidence: float = Field(default=0, ge=0, le=1)
    abstain_calibrator_path: Path = Path("./abstention_calibrator.json")
    allow_unregistered_fetch_for_baseline: bool = False
    spend_cap_usd: float = Field(default=0, ge=0)
    llm_price_table_json: dict[str, dict[str, float]] = Field(default_factory=dict)
    playbook_dir: Path = Path("./playbooks")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw_url = value.get_secret_value()
        try:
            parsed = make_url(raw_url)
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL must be a valid PostgreSQL SQLAlchemy URL") from exc
        if parsed.get_backend_name() != "postgresql":
            raise ValueError("DATABASE_URL must use PostgreSQL")
        if parsed.drivername not in {"postgresql", "postgresql+psycopg"}:
            raise ValueError("DATABASE_URL must use the SQLAlchemy psycopg driver")
        return value

    @field_validator("service_token")
    @classmethod
    def validate_service_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("SERVICE_TOKEN must be non-empty")
        return value

    @field_validator(
        "agent_api_url",
        "graph_api_url",
        "workbench_api_url",
        "novnc_base_url",
        "graph_mock_api_url",
        "workbench_mock_api_url",
    )
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("service URLs must be absolute http or https URLs")
        return value.rstrip("/")

    @field_validator("blob_url")
    @classmethod
    def validate_blob_url(cls, value: str) -> str:
        if not (value.startswith("file://") or value.startswith("s3://")):
            raise ValueError("BLOB_URL must use file:// or s3://")
        return value

    @field_validator("agent_user_agent")
    @classmethod
    def validate_user_agent(cls, value: str) -> str:
        if not value.strip() or "\r" in value or "\n" in value:
            raise ValueError("AGENT_USER_AGENT must be a non-empty single-line value")
        if "contact:" not in value.lower():
            raise ValueError("AGENT_USER_AGENT must include a contact")
        return value

    @field_validator("session_enc_key", "vault_file_key")
    @classmethod
    def validate_aes_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("encryption keys must be base64-encoded 32-byte values") from exc
        if len(decoded) != 32:
            raise ValueError("encryption keys must be base64-encoded 32-byte values")
        return value

    @field_validator("llm_price_table_json", mode="before")
    @classmethod
    def parse_llm_price_table(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("LLM_PRICE_TABLE_JSON must be valid JSON") from exc
        return value

    @field_validator("llm_price_table_json")
    @classmethod
    def validate_llm_price_table(
        cls, value: dict[str, dict[str, float]]
    ) -> dict[str, dict[str, float]]:
        required_rates = {"in_per_mtok", "out_per_mtok"}
        for model, rates in value.items():
            if not model.strip() or set(rates) != required_rates:
                raise ValueError("LLM_PRICE_TABLE_JSON values require in_per_mtok and out_per_mtok")
            if any(not math.isfinite(rate) or rate < 0 for rate in rates.values()):
                raise ValueError("LLM_PRICE_TABLE_JSON rates must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_cross_field_requirements(self) -> Settings:
        if self.gba_env == "prod" and self.peer_mode == "mock":
            raise ValueError("PEER_MODE=mock is forbidden in production")
        if self.support_judge in {"llm", "both"}:
            if not self.llm_model_judge:
                raise ValueError("LLM_MODEL_JUDGE is required when SUPPORT_JUDGE includes llm")
            if self.llm_model_planner == self.llm_model_judge:
                raise ValueError("LLM_MODEL_JUDGE must differ from LLM_MODEL_PLANNER")
        if self.llm_mode == "live" and self.llm_api_key is None:
            raise ValueError("LLM_API_KEY is required when LLM_MODE=live")
        if self.llm_provider == "openai_compatible" and self.llm_mode == "live":
            if self.llm_base_url is None:
                raise ValueError("LLM_BASE_URL is required for a live openai_compatible provider")
            self.validate_http_url(self.llm_base_url)
        if self.vault_backend == "file":
            if self.vault_file is None or self.vault_file_key is None:
                raise ValueError(
                    "VAULT_FILE and VAULT_FILE_KEY are required when VAULT_BACKEND=file"
                )
        if self.blob_url.startswith("s3://"):
            if self.blob_s3_key is None or self.blob_s3_secret is None:
                raise ValueError(
                    "BLOB_S3_KEY and BLOB_S3_SECRET are required for an s3:// BLOB_URL"
                )
        if self.gba_env == "prod" and self.allow_unregistered_fetch_for_baseline:
            raise ValueError(
                "ALLOW_UNREGISTERED_FETCH_FOR_BASELINE cannot be enabled when GBA_ENV=prod"
            )
        return self

    def require_session_encryption_key(self) -> bytes:
        """Return the decoded session key or fail when encrypted sessions are requested."""
        if self.session_enc_key is None:
            raise ValueError("SESSION_ENC_KEY is required before a browser session can be used")
        return base64.b64decode(self.session_enc_key.get_secret_value(), validate=True)

    def validate_registry_path(self) -> Path:
        """Ensure the configured registry file is present before service startup."""
        if not self.registry_path.is_file():
            raise ValueError(f"REGISTRY_PATH does not exist: {self.registry_path}")
        return self.registry_path


@lru_cache
def get_settings() -> Settings:
    """Return cached process settings."""
    return Settings()  # type: ignore[call-arg]
