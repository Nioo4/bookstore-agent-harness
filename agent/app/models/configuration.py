from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.gateway import gateway_url

AGENT_ROOT = Path(__file__).resolve().parents[2]
CURRENT_PROMPT_SET = "v4.2-single-agent-rag"


def duration_seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m)", value)
    if match is None:
        raise ValueError("model timeout must use a positive duration")
    result = float(match[1]) * {"ms": .001, "s": 1, "m": 60}[match[2]]
    if result <= 0:
        raise ValueError("model timeout must be positive")
    return result


class ManifestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=AGENT_ROOT.parent / ".env", extra="ignore", hide_input_in_errors=True)
    model_profile_manifest_path: str = "config/model-profiles.yaml"
    prompt_manifest_path: str = "prompts/manifest.yaml"
    pricing_manifest_path: str = "pricing/pricing-v1.json"
    eval_artifact_dir: str = "evals/reports"


class EmbeddingSettings(ManifestSettings):
    """Settings shared by the API and the explicit RAG index command.

    Keeping embedding configuration separate from chat, database, Redis and
    JWT settings lets an offline corpus build validate only the dependencies
    it actually uses.
    """

    embedding_base_url: str
    embedding_api_key: SecretStr
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_dimensions: int = Field(default=1024, ge=1)
    embedding_batch_size: int = Field(default=20, ge=1, le=20)
    embedding_timeout: str = "30s"
    environment: Literal["development", "test", "production"] = "development"

    @field_validator("embedding_base_url")
    @classmethod
    def validate_gateway(cls, value: str) -> str:
        return gateway_url(value)

    @field_validator("embedding_api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if (not raw or raw.lower() in {"secret", "replace-me", "change_me", "changeme", "your-api-key", "[redacted]"}
                or raw.startswith("<") or raw.lower().startswith("replace-with")):
            raise ValueError("a non-placeholder model credential is required")
        return value

    @model_validator(mode="after")
    def validate_embedding_configuration(self) -> "EmbeddingSettings":
        duration_seconds(self.embedding_timeout)
        if self.environment != "test":
            if self.embedding_dimensions != 1024 or self.embedding_model != "qwen3.7-text-embedding":
                raise ValueError("live embedding configuration requires qwen3.7-text-embedding at 1024 dimensions")
            if not self.embedding_base_url.startswith("https://"):
                raise ValueError("live model gateways require HTTPS")
        return self


class ModelSettings(EmbeddingSettings):
    model_base_url: str = "https://api.deepseek.com"
    model_api_key: SecretStr
    model_adapter: Literal["deepseek-chat-json-v1", "openai-json-schema-v1"] = "deepseek-chat-json-v1"
    model_profile: str = "deepseek-flash-v2"
    prompt_set_version: str = CURRENT_PROMPT_SET
    agent_model: str = "deepseek-v4-flash"
    model_max_output_tokens: int = Field(default=8192, ge=1, le=8192)
    model_timeout: str = "30s"

    @field_validator("model_base_url")
    @classmethod
    def validate_model_gateway(cls, value: str) -> str:
        return gateway_url(value)

    @field_validator("model_api_key")
    @classmethod
    def validate_model_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value().strip()
        if (not raw or raw.lower() in {"secret", "replace-me", "change_me", "changeme", "your-api-key", "[redacted]"}
                or raw.startswith("<") or raw.lower().startswith("replace-with")):
            raise ValueError("a non-placeholder model credential is required")
        return value

    @model_validator(mode="after")
    def validate_model_configuration(self) -> "ModelSettings":
        duration_seconds(self.model_timeout)
        if self.environment != "test":
            if self.model_adapter != "deepseek-chat-json-v1":
                raise ValueError("legacy adapter is only permitted in an explicit test environment")
            if not self.model_base_url.startswith("https://"):
                raise ValueError("live model gateways require HTTPS")
            if self.model_base_url == self.embedding_base_url:
                raise ValueError("chat and embedding require separate live gateways")
        return self
