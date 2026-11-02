import pytest
from pydantic import ValidationError

from app.config import AgentAPISettings, RagCommandSettings, WorkerSettings


def api_kwargs() -> dict[str, object]:
    return {
        "_env_file": None,
        "model_api_key": "synthetic-chat-test-key",
        "embedding_api_key": "synthetic-embedding-test-key",
        "embedding_base_url": "https://embedding.example/v1",
        "redis_addr": "redis://redis:6379/0",
        "qdrant_url": "http://qdrant:6333",
        "agent_mysql_dsn": "mysql://agent:secret@mysql:3306/bookstore",
        "jwt_public_key_path": "../deploy/keys/jwt-public.pem",
        "environment": "test",
    }


def test_v2_api_uses_shared_bookstore_issuer_and_fixed_confirmation_ttl() -> None:
    settings = AgentAPISettings(**api_kwargs())
    assert settings.jwt_issuer == "bookstore"
    assert "confirmation_binding_ttl" not in AgentAPISettings.model_fields
    assert settings.confirmation_binding_ttl_seconds == 24 * 60 * 60


def test_worker_requires_only_worker_dependencies() -> None:
    settings = WorkerSettings(
        _env_file=None,
        worker_mysql_dsn="mysql://worker:secret@mysql:3306/bookstore",
        qdrant_url="http://qdrant:6333",
    )
    assert settings.worker_mysql_dsn_value.startswith("mysql://worker:")
    assert settings.outbox_poll_interval_seconds == 1
    assert not hasattr(settings, "agent_model")
    assert not hasattr(settings, "inventory_stream")
    assert settings.legacy_memory_collection == "bookstore_memory_qwen37_1024_v1"
    assert not hasattr(settings, "memory_collection")
    assert not hasattr(settings, "trace_purge_grace")


def test_rag_command_has_no_chat_or_service_credentials() -> None:
    settings = RagCommandSettings(
        _env_file=None,
        embedding_api_key="synthetic-embedding-test-key",
        embedding_base_url="https://embedding.example/v1",
        qdrant_url="http://qdrant:6333",
        environment="test",
    )
    assert settings.business_grpc_addr == "bookstore-service:9090"
    assert settings.policy_collection_alias
    assert not hasattr(settings, "model_api_key")
    assert not hasattr(settings, "redis_addr")
    assert not hasattr(settings, "agent_mysql_dsn")
    assert not hasattr(settings, "jwt_public_key_path")


def test_api_rejects_invalid_limits_and_production_proxy_configuration() -> None:
    with pytest.raises(ValidationError):
        AgentAPISettings(**api_kwargs(), agent_max_rounds=7)
    with pytest.raises(ValidationError, match="production guest chat"):
        AgentAPISettings(**{**api_kwargs(), "environment": "production", "build_commit": "abcdef1"})
