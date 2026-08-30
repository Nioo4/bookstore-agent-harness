from __future__ import annotations

import re
import ipaddress
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.models.configuration import ModelSettings


class AgentSettings(ModelSettings):

    catalog_rpc_addr: str = Field(min_length=1)
    order_rpc_addr: str = Field(min_length=1)
    redis_addr: str = Field(min_length=1)
    qdrant_url: str = Field(min_length=1)
    agent_mysql_dsn: SecretStr
    jwt_public_key_path: str = Field(min_length=1)
    jwt_issuer: str = Field(default="bookstore-auth", min_length=1)
    environment: Literal["development", "test", "production"] = "development"
    build_commit: str = "development"

    agent_max_nodes: int = Field(default=12, ge=1, le=12)
    agent_max_depth: int = Field(default=4, ge=1, le=4)
    agent_max_parallel: int = Field(default=3, ge=1, le=3)
    agent_max_tool_calls: int = Field(default=20, ge=1, le=20)
    confirmation_ttl: str = "10m"
    confirmation_binding_ttl: str = "24h"
    memory_inferred_min_confidence: float = Field(default=0.85, ge=0.85, le=1)
    inventory_stream: str = Field(default="inventory.events", min_length=1)
    inventory_stream_group: str = Field(default="inventory-watch-workers", min_length=1)
    inventory_stream_dlq: str = Field(default="inventory.events.dlq", min_length=1)
    inventory_stream_block: str = "5s"
    inventory_stream_batch_size: int = Field(default=20, ge=1, le=20)
    inventory_stream_claim_idle: str = "60s"
    inventory_event_max_attempts: int = Field(default=10, ge=1)
    inventory_event_retention: str = "7d"
    inventory_watch_default_ttl: str = "30d"
    inventory_watch_max_ttl: str = "90d"
    inventory_watch_expiry_scan_interval: str = "60s"
    trace_retention_days: int = Field(default=30, ge=1)
    trace_purge_interval: str = "1h"
    trace_purge_grace: str = "24h"
    trace_max_json_bytes: int = Field(default=65_536, ge=1024)
    trace_max_final_answer_bytes: int = Field(default=32_768, ge=1024)
    trace_max_error_bytes: int = Field(default=2_048, ge=256)
    trace_max_evidence_bytes: int = Field(default=8_192, ge=512)
    trace_max_run_bytes: int = Field(default=2_097_152, ge=65_536)
    trace_step_page_size: int = Field(default=100, ge=1, le=200)
    replay_max_per_user_per_hour: int = Field(default=5, ge=1)
    replay_max_concurrent_per_user: int = Field(default=1, ge=1, le=1)
    replay_lease_ttl: str = "60s"
    model_profile_manifest_path: str = "config/model-profiles.yaml"
    prompt_manifest_path: str = "prompts/manifest.yaml"
    pricing_manifest_path: str = "pricing/pricing-v1.json"
    eval_retention_days: int = Field(default=180, ge=1)
    eval_artifact_dir: str = "evals/reports"
    llm_judge_enabled: bool = False
    judge_model_profile: str = "deepseek-pro-v1"
    judge_prompt_version: str = "recommendation-v1"
    otel_trace_retention_days: int = Field(default=7, ge=1)
    guest_chat_requests_per_hour: int = Field(default=20, ge=1)
    guest_chat_max_concurrent_per_ip: int = Field(default=2, ge=1)
    guest_chat_timeout: str = "120s"
    guest_rate_limit_hmac_key: SecretStr | None = None
    trusted_proxy_cidrs: str = ""

    @model_validator(mode="after")
    def validate_confirmation_ttls(self) -> "AgentSettings":
        action_seconds = _duration_seconds(self.confirmation_ttl)
        binding_seconds = _duration_seconds(self.confirmation_binding_ttl)
        if binding_seconds < 24 * 60 * 60 or binding_seconds < action_seconds:
            raise ValueError("confirmation binding TTL must cover the action TTL and be at least 24h")
        if _duration_seconds(self.inventory_watch_default_ttl) > _duration_seconds(self.inventory_watch_max_ttl):
            raise ValueError("inventory watch default TTL must not exceed maximum TTL")
        _duration_seconds(self.inventory_stream_block)
        _duration_seconds(self.inventory_stream_claim_idle)
        _duration_seconds(self.inventory_event_retention)
        _duration_seconds(self.inventory_watch_expiry_scan_interval)
        _duration_seconds(self.trace_purge_interval)
        _duration_seconds(self.trace_purge_grace)
        _duration_seconds(self.replay_lease_ttl)
        guest_timeout = _duration_seconds(self.guest_chat_timeout)
        if guest_timeout > 120:
            raise ValueError("guest chat timeout must not exceed 120s")
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
        return _duration_seconds(self.confirmation_binding_ttl)

    @property
    def inventory_stream_block_ms(self) -> int:
        return _duration_seconds(self.inventory_stream_block) * 1000

    @property
    def inventory_stream_claim_idle_ms(self) -> int:
        return _duration_seconds(self.inventory_stream_claim_idle) * 1000

    @property
    def inventory_event_retention_seconds(self) -> int:
        return _duration_seconds(self.inventory_event_retention)

    @property
    def inventory_watch_expiry_scan_seconds(self) -> int:
        return _duration_seconds(self.inventory_watch_expiry_scan_interval)

    @property
    def inventory_watch_default_ttl_seconds(self) -> int:
        return _duration_seconds(self.inventory_watch_default_ttl)

    @property
    def inventory_watch_max_ttl_seconds(self) -> int:
        return _duration_seconds(self.inventory_watch_max_ttl)

    @property
    def trace_purge_interval_seconds(self) -> int:
        return _duration_seconds(self.trace_purge_interval)

    @property
    def trace_purge_grace_seconds(self) -> int:
        return _duration_seconds(self.trace_purge_grace)

    @property
    def replay_lease_ttl_seconds(self) -> int:
        return _duration_seconds(self.replay_lease_ttl)

    @property
    def guest_chat_timeout_seconds(self) -> int:
        return _duration_seconds(self.guest_chat_timeout)

    @property
    def trusted_proxy_networks(self) -> tuple[str, ...]:
        return tuple(item.strip() for item in self.trusted_proxy_cidrs.split(",") if item.strip())


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", value.strip().casefold())
    if match is None:
        raise ValueError("duration must use a positive integer followed by s, m, h, or d")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return amount * multiplier
