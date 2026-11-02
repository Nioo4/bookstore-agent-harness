from __future__ import annotations

import ipaddress
import re
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.configuration import AGENT_ROOT, EmbeddingSettings, ModelSettings


# Confirmation bindings outlive the MySQL 10-minute pending action so a lost
# response can be retried with the same decision. This is a protocol invariant.
CONFIRMATION_BINDING_TTL_SECONDS = 24 * 60 * 60


class AgentAPISettings(ModelSettings):
    """Configuration used by the v2 HTTP Agent API only."""

    business_grpc_addr: str = Field(default="bookstore-service:9090", min_length=1)
    redis_addr: str = Field(min_length=1)
    qdrant_url: str = Field(min_length=1)
    agent_mysql_dsn: SecretStr
    jwt_public_key_path: str = Field(min_length=1)
    jwt_issuer: str = Field(default="bookstore", min_length=1)
    environment: Literal["development", "test", "production"] = "development"
    build_commit: str = "development"

    agent_max_rounds: int = Field(default=6, ge=1, le=6)
    agent_max_tool_calls: int = Field(default=12, ge=1, le=12)
    agent_max_parallel_reads: int = Field(default=3, ge=1, le=3)
    agent_request_timeout: str = "120s"
    rag_max_context_tokens: int = Field(default=6000, ge=1, le=6000)
    policy_collection_alias: str = Field(default="bookstore_policy_current", min_length=1)
    book_collection_alias: str = Field(default="bookstore_book_knowledge_current", min_length=1)
    rag_index_manifest_path: str = "runtime/rag-index-manifest.json"
    confirmation_ttl: str = "10m"
    inventory_watch_default_ttl: str = "30d"
    inventory_watch_max_ttl: str = "90d"

    trace_retention_days: int = Field(default=30, ge=1)
    trace_max_json_bytes: int = Field(default=65_536, ge=1024)
    trace_max_final_answer_bytes: int = Field(default=32_768, ge=1024)
    trace_max_error_bytes: int = Field(default=2_048, ge=256)
    trace_max_evidence_bytes: int = Field(default=8_192, ge=512)
    trace_max_run_bytes: int = Field(default=2_097_152, ge=65_536)

    guest_chat_requests_per_hour: int = Field(default=20, ge=1)
    guest_chat_max_concurrent_per_ip: int = Field(default=2, ge=1)
    guest_chat_timeout: str = "120s"
    guest_rate_limit_hmac_key: SecretStr | None = None
    trusted_proxy_cidrs: str = ""

    @model_validator(mode="after")
    def validate_v2_api(self) -> "AgentAPISettings":
        action_seconds = _duration_seconds(self.confirmation_ttl)
        if CONFIRMATION_BINDING_TTL_SECONDS < 24 * 60 * 60 or CONFIRMATION_BINDING_TTL_SECONDS < action_seconds:
            raise ValueError("confirmation binding TTL must cover the action TTL and be at least 24h")
        if _duration_seconds(self.inventory_watch_default_ttl) > _duration_seconds(self.inventory_watch_max_ttl):
            raise ValueError("inventory watch default TTL must not exceed maximum TTL")
        if self.agent_request_timeout_seconds > 120 or self.guest_chat_timeout_seconds > 120:
            raise ValueError("Agent request and guest chat timeout must not exceed 120s")
        if self.trace_max_run_bytes < self.trace_max_json_bytes:
            raise ValueError("trace run limit must cover the per-JSON limit")
        proxies = tuple(item.strip() for item in self.trusted_proxy_cidrs.split(",") if item.strip())
        for value in proxies:
            ipaddress.ip_network(value, strict=False)
        key = self.guest_rate_limit_hmac_key
        if key is not None and len(key.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("guest rate-limit HMAC key must be at least 32 bytes")
        if self.environment == "production" and (key is None or not proxies):
            raise ValueError("production guest chat requires an HMAC key and trusted proxy CIDRs")
        if self.environment == "production" and not re.fullmatch(r"[0-9a-f]{7,40}", self.build_commit):
            raise ValueError("production BUILD_COMMIT must be a 7-40 character lowercase Git commit")
        return self

    @property
    def harness_version(self) -> str:
        return f"0.1.0+{self.build_commit}"

    @property
    def confirmation_binding_ttl_seconds(self) -> int:
        return CONFIRMATION_BINDING_TTL_SECONDS

    @property
    def inventory_watch_default_ttl_seconds(self) -> int:
        return _duration_seconds(self.inventory_watch_default_ttl)

    @property
    def inventory_watch_max_ttl_seconds(self) -> int:
        return _duration_seconds(self.inventory_watch_max_ttl)

    @property
    def agent_request_timeout_seconds(self) -> int:
        return _duration_seconds(self.agent_request_timeout)

    @property
    def guest_chat_timeout_seconds(self) -> int:
        return _duration_seconds(self.guest_chat_timeout)

    @property
    def trusted_proxy_networks(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.trusted_proxy_cidrs.split(",") if item.strip())


class RagCommandSettings(EmbeddingSettings):
    """Minimal settings for the explicit offline RAG index command."""

    business_grpc_addr: str = Field(default="bookstore-service:9090", min_length=1)
    qdrant_url: str = Field(min_length=1)
    policy_collection_alias: str = Field(default="bookstore_policy_current", min_length=1)
    book_collection_alias: str = Field(default="bookstore_book_knowledge_current", min_length=1)
    rag_index_manifest_path: str = "runtime/rag-index-manifest.json"


class WorkerSettings(BaseSettings):
    """Minimal configuration for the MySQL outbox/retention worker."""

    model_config = SettingsConfigDict(
        env_file=AGENT_ROOT.parent / ".env", extra="ignore", hide_input_in_errors=True
    )

    worker_mysql_dsn: SecretStr
    qdrant_url: str = Field(min_length=1)
    # Only the historical EPISODE delete path may use this collection. v2
    # never indexes, upserts, queries, or creates EPISODE records.
    legacy_memory_collection: str = Field(default="bookstore_memory_qwen37_1024_v1", min_length=1)
    outbox_poll_interval: str = "1s"
    outbox_batch_size: int = Field(default=50, ge=1, le=50)
    outbox_lease_ttl: str = "30s"
    outbox_max_attempts: int = Field(default=10, ge=1, le=10)
    trace_retention_days: int = Field(default=30, ge=1)
    retention_scan_interval: str = "60s"

    @model_validator(mode="after")
    def validate_worker(self) -> "WorkerSettings":
        _duration_seconds(self.outbox_poll_interval)
        _duration_seconds(self.outbox_lease_ttl)
        _duration_seconds(self.retention_scan_interval)
        return self

    @property
    def worker_mysql_dsn_value(self) -> str:
        return self.worker_mysql_dsn.get_secret_value()

    @property
    def outbox_poll_interval_seconds(self) -> float:
        return float(_duration_seconds(self.outbox_poll_interval))

    @property
    def outbox_lease_ttl_seconds(self) -> int:
        return _duration_seconds(self.outbox_lease_ttl)

    @property
    def retention_scan_interval_seconds(self) -> float:
        return float(_duration_seconds(self.retention_scan_interval))


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip().casefold())
    if match is None:
        raise ValueError("duration must use a positive integer followed by s, m, h, or d")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount * multiplier
