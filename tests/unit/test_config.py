"""Configuration validation tests."""

from __future__ import annotations

import base64

import pytest
from pydantic import SecretStr, ValidationError

from gba_agent.config import Settings


def valid_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": SecretStr("postgresql+psycopg://user:pass@localhost:5433/agentdb"),
        "service_token": SecretStr("test-token"),
        "agent_user_agent": "GuardedLegalAgent/1.0 (contact: test@example.com)",
        "llm_provider": "openai_compatible",
        "llm_api_key": SecretStr("test-key"),
        "llm_base_url": "http://localhost:4000/v1",
        "llm_mode": "replay",
        "llm_model_planner": "planner-test",
        "llm_model_judge": "judge-test",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_settings_load_defaults_and_validate_documented_urls() -> None:
    settings = valid_settings()

    assert settings.database_url.get_secret_value().startswith("postgresql+psycopg://")
    assert settings.graph_api_url == "http://localhost:8101"
    assert settings.workbench_api_url == "http://localhost:8102"
    assert settings.browser_mode == "headless"
    assert settings.support_judge == "llm"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", SecretStr("sqlite:///agent.db")),
        ("graph_api_url", "file:///tmp/graph"),
        ("workbench_api_url", "localhost:8102"),
        ("agent_user_agent", "Agent"),
        ("service_token", SecretStr(" ")),
        ("session_enc_key", SecretStr("not-base64")),
        ("session_enc_key", SecretStr(base64.b64encode(b"short").decode())),
        ("browser_mode", "stealth"),
        ("support_judge", "model"),
        ("support_min_score", 1.1),
        ("llm_price_table_json", {"model": {"in_per_mtok": -1, "out_per_mtok": 1}}),
        ("llm_price_table_json", {"model": {"input": 1, "output": 1}}),
    ],
)
def test_invalid_config_values_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        valid_settings(**{field: value})


def test_required_credentials_have_no_unsafe_defaults() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_judge_model_must_differ_from_planner() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        valid_settings(llm_model_judge="planner-test")


def test_live_mode_requires_api_key() -> None:
    with pytest.raises(ValidationError, match="LLM_API_KEY"):
        valid_settings(llm_mode="live", llm_api_key=None)


def test_file_vault_requires_encrypted_file_configuration() -> None:
    with pytest.raises(ValidationError, match="VAULT_FILE"):
        valid_settings(vault_backend="file")


def test_production_disallows_unregistered_baseline_fetch() -> None:
    with pytest.raises(ValidationError, match="cannot be enabled"):
        valid_settings(gba_env="prod", allow_unregistered_fetch_for_baseline=True)


def test_session_key_is_required_only_when_session_use_is_requested() -> None:
    settings = valid_settings()
    with pytest.raises(ValueError, match="SESSION_ENC_KEY"):
        settings.require_session_encryption_key()

    key = base64.b64encode(b"k" * 32).decode()
    assert valid_settings(session_enc_key=SecretStr(key)).require_session_encryption_key() == (
        b"k" * 32
    )
