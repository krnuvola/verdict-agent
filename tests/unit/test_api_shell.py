"""A-0 API shell tests."""

from gba_agent.api.main import health


def test_health_uses_shared_contract_shape() -> None:
    assert health() == {
        "status": "ok",
        "service": "agent-api",
        "version": "0.1.0",
        "contract_version": "1.0.0",
    }
