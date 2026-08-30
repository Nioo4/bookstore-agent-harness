from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.trace.errors import TraceError
from app.trace.types import ManifestReference, canonical_json


class PromptFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromptSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=64)
    prompts: list[PromptFile] = Field(min_length=1)


class PromptManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "v1"
    prompt_sets: list[PromptSet] = Field(min_length=1)


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    router: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    verifier: str = Field(min_length=1)


class ModelProfileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "v1"
    profiles: list[ModelProfile] = Field(min_length=1)


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_microusd_per_million: int = Field(ge=0)
    output_microusd_per_million: int = Field(ge=0)
    cached_microusd_per_million: int | None = Field(default=None, ge=0)
    reasoning_microusd_per_million: int | None = Field(default=None, ge=0)


class PricingManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "v1"
    pricing_version: str = Field(min_length=1, max_length=64)
    effective_at: str = Field(min_length=1)
    models: dict[str, ModelPrice]


class VersionRegistry:
    def __init__(
        self,
        root: Path,
        prompt_manifest: PromptManifest,
        model_manifest: ModelProfileManifest,
        pricing_manifest: PricingManifest,
    ) -> None:
        self.root = root
        self.prompt_manifest = prompt_manifest
        self.model_manifest = model_manifest
        self.pricing_manifest = pricing_manifest
        self.prompt_manifest_hash = self.prompt_set_hash("v1")
        self.model_manifest_hash = self.model_profile_hash("default")
        self.pricing_manifest_hash = _manifest_hash(pricing_manifest.model_dump(mode="json"))
        self.pricing_versions = self._load_pricing_versions()
        self.tool_registry_versions = self._load_tool_registry_versions()

    @classmethod
    def load(
        cls,
        *,
        root: Path,
        prompt_manifest_path: Path,
        model_manifest_path: Path,
        pricing_manifest_path: Path,
    ) -> "VersionRegistry":
        resolved_root = root.resolve(strict=True)
        prompt_path = _inside(resolved_root, prompt_manifest_path)
        model_path = _inside(resolved_root, model_manifest_path)
        pricing_path = _inside(resolved_root, pricing_manifest_path)
        try:
            prompt = PromptManifest.model_validate(yaml.safe_load(prompt_path.read_text(encoding="utf-8")))
            models = ModelProfileManifest.model_validate(yaml.safe_load(model_path.read_text(encoding="utf-8")))
            pricing = PricingManifest.model_validate(json.loads(pricing_path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise TraceError("TRACE_DEPENDENCY_UNAVAILABLE", "version manifest validation failed", retryable=True) from exc
        registry = cls(resolved_root, prompt, models, pricing)
        registry._verify_prompt_files()
        return registry

    def prompt_set(self, version: str) -> PromptSet:
        for item in self.prompt_manifest.prompt_sets:
            if item.version == version:
                return item
        raise TraceError("PROMPT_VERSION_NOT_FOUND", "prompt version is not allowed")

    def model_profile(self, name: str) -> ModelProfile:
        for item in self.model_manifest.profiles:
            if item.name == name:
                return item
        raise TraceError("MODEL_PROFILE_NOT_ALLOWED", "model profile is not allowed")

    def prompt_text(self, version: str, role: str) -> str:
        prompt_set = self.prompt_set(version)
        for item in prompt_set.prompts:
            if item.role == role:
                return _inside(self.root, self.root / item.path).read_text(encoding="utf-8")
        raise TraceError("PROMPT_VERSION_NOT_FOUND", "prompt role is missing from the version")

    def prompt_set_hash(self, version: str) -> bytes:
        return _manifest_hash(self.prompt_set(version).model_dump(mode="json"))

    def model_profile_hash(self, name: str) -> bytes:
        return _manifest_hash(self.model_profile(name).model_dump(mode="json"))

    def assert_database_references(self, references: list[ManifestReference]) -> None:
        for reference in references:
            try:
                self.model_profile(reference.model_profile)
                self.prompt_set(reference.prompt_set_version)
            except TraceError as exc:
                raise TraceError(
                    "TRACE_DEPENDENCY_UNAVAILABLE",
                    "a referenced named manifest artifact is unavailable",
                    retryable=True,
                ) from exc
            if reference.model_manifest_hash != self.model_profile_hash(reference.model_profile):
                raise TraceError(
                    "TRACE_DEPENDENCY_UNAVAILABLE",
                    "a referenced model manifest artifact is unavailable",
                    retryable=True,
                )
            if reference.prompt_manifest_hash != self.prompt_set_hash(reference.prompt_set_version):
                raise TraceError(
                    "TRACE_DEPENDENCY_UNAVAILABLE",
                    "a referenced prompt manifest artifact is unavailable",
                    retryable=True,
                )
            if reference.tool_registry_version not in self.tool_registry_versions.values():
                raise TraceError(
                    "TRACE_DEPENDENCY_UNAVAILABLE",
                    "a referenced tool registry artifact is unavailable",
                    retryable=True,
                )
            if (
                reference.pricing_version is not None
                and reference.pricing_version not in self.pricing_versions
            ):
                raise TraceError(
                    "TRACE_DEPENDENCY_UNAVAILABLE",
                    "a referenced pricing manifest artifact is unavailable",
                    retryable=True,
                )

    def _load_pricing_versions(self) -> dict[str, PricingManifest]:
        values: dict[str, PricingManifest] = {}
        try:
            for path in sorted((self.root / "pricing").glob("*.json")):
                manifest = PricingManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
                if manifest.pricing_version in values:
                    raise ValueError("duplicate pricing version")
                values[manifest.pricing_version] = manifest
        except Exception as exc:
            raise TraceError(
                "TRACE_DEPENDENCY_UNAVAILABLE",
                "pricing manifest archive validation failed",
                retryable=True,
            ) from exc
        if self.pricing_manifest.pricing_version not in values:
            raise TraceError(
                "TRACE_DEPENDENCY_UNAVAILABLE",
                "current pricing manifest is not retained in the pricing archive",
                retryable=True,
            )
        return values

    def _load_tool_registry_versions(self) -> dict[str, str]:
        try:
            payload = json.loads(
                (self.root / "config/tool-registry-versions.json").read_text(encoding="utf-8")
            )
            if payload.get("schema_version") != "v1" or not isinstance(payload.get("versions"), dict):
                raise ValueError("invalid tool registry version archive")
            values = {str(name): str(value) for name, value in payload["versions"].items()}
            if not values or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in values.values()):
                raise ValueError("invalid tool registry version hash")
            return values
        except Exception as exc:
            raise TraceError(
                "TRACE_DEPENDENCY_UNAVAILABLE",
                "tool registry version archive validation failed",
                retryable=True,
            ) from exc

    def _verify_prompt_files(self) -> None:
        seen_versions: set[str] = set()
        for prompt_set in self.prompt_manifest.prompt_sets:
            if prompt_set.version in seen_versions:
                raise TraceError("TRACE_DEPENDENCY_UNAVAILABLE", "duplicate prompt version", retryable=True)
            seen_versions.add(prompt_set.version)
            roles: set[str] = set()
            for item in prompt_set.prompts:
                if item.role in roles:
                    raise TraceError("TRACE_DEPENDENCY_UNAVAILABLE", "duplicate prompt role", retryable=True)
                roles.add(item.role)
                path = _inside(self.root, self.root / item.path)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != item.sha256:
                    raise TraceError("TRACE_DEPENDENCY_UNAVAILABLE", "prompt hash mismatch", retryable=True)


def _inside(root: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise TraceError("TRACE_DEPENDENCY_UNAVAILABLE", "manifest path escapes the config root", retryable=True)
    return resolved


def _manifest_hash(value: Any) -> bytes:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).digest()
