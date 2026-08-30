from __future__ import annotations

import hashlib
import json

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.models.configuration import AGENT_ROOT, ModelSettings


class VectorSpace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str
    base_url: str
    model: str
    dimensions: int = Field(gt=0)
    distance: str
    policy_collection: str
    memory_collection: str
    test_only: bool

    @property
    def identity(self) -> str:
        # Public configuration identity, never includes a credential.
        value = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()


def selected_vector_space(settings: ModelSettings) -> VectorSpace:
    manifest = yaml.safe_load((AGENT_ROOT / "config/vector-spaces.yaml").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "v1":
        raise ValueError("unsupported vector space manifest")
    spaces = [VectorSpace.model_validate(item) for item in manifest["spaces"]]
    collections = [name for space in spaces for name in (space.policy_collection, space.memory_collection)]
    if len(collections) != len(set(collections)):
        raise ValueError("vector collections must bind to one retained space")
    for space in spaces:
        if (space.base_url, space.model, space.dimensions, space.policy_collection, space.memory_collection) == (
            settings.embedding_base_url, settings.embedding_model, settings.embedding_dimensions,
            settings.policy_collection, settings.memory_collection,
        ):
            if space.test_only != (settings.environment == "test") or space.distance != "Cosine":
                raise ValueError("vector space environment or distance mismatch")
            return space
    raise ValueError("embedding workspace/model/dimensions/collections do not match a retained vector space")
