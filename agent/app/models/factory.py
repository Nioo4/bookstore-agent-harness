from __future__ import annotations

import httpx

from app.models.configuration import AGENT_ROOT, ManifestSettings, ModelSettings, duration_seconds
from app.models.deepseek import DeepSeekJSONProvider
from app.models.embeddings import OpenAICompatibleEmbeddingProvider
from app.models.openai_compatible import OpenAICompatibleProvider
from app.trace.manifests import VersionRegistry
from app.models.vector_space import selected_vector_space


def load_versions(settings: ManifestSettings) -> VersionRegistry:
    return VersionRegistry.load(
        root=AGENT_ROOT,
        prompt_manifest_path=AGENT_ROOT / settings.prompt_manifest_path,
        model_manifest_path=AGENT_ROOT / settings.model_profile_manifest_path,
        pricing_manifest_path=AGENT_ROOT / settings.pricing_manifest_path,
    )


class ModelRuntime:
    """One configuration factory for API, workers, indexer, replay and CLI."""
    def __init__(self, settings: ModelSettings) -> None:
        self.settings = settings
        self.versions = load_versions(settings)
        self.vector_space = selected_vector_space(settings)
        self.profile = self.versions.model_profile(settings.model_profile)
        self.versions.prompt_set(settings.prompt_set_version)
        if (self.profile.router, self.profile.agent, self.profile.verifier) != (
                settings.router_model, settings.agent_model, settings.verifier_model):
            raise ValueError("configured model IDs do not match the selected profile")
        for role in ("router", "catalog", "recommendation", "knowledge", "order", "verifier"):
            self.prompt(role)

    def prompt(self, role: str) -> str:
        return self.versions.prompt_text(self.settings.prompt_set_version, role)

    def chat(self, *, client: httpx.AsyncClient | None = None):
        s = self.settings
        if s.model_adapter == "deepseek-chat-json-v1":
            return DeepSeekJSONProvider(s.model_base_url, s.model_api_key.get_secret_value(),
                duration_seconds(s.model_timeout), max_output_tokens=s.model_max_output_tokens, client=client)
        return OpenAICompatibleProvider(s.model_base_url, s.model_api_key.get_secret_value(),
            duration_seconds(s.model_timeout), max_output_tokens=s.model_max_output_tokens, client=client)

    def embeddings(self, *, client: httpx.AsyncClient | None = None):
        s = self.settings
        return OpenAICompatibleEmbeddingProvider(s.embedding_base_url, s.embedding_api_key.get_secret_value(),
            duration_seconds(s.embedding_timeout), dimensions=s.embedding_dimensions,
            batch_size=s.embedding_batch_size, client=client)
