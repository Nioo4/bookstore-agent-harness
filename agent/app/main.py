from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from redis.asyncio import Redis

from app.agents.registry import build_agent_registry
from app.agents.verification import DeterministicVerifier, LLMVerifier, RepairingVerifier, VerificationPipeline
from app.api.app import AppDependencies, create_app
from app.api.auth import AccessTokenVerifier
from app.api.chat import ChatService
from app.api.confirmation import ConfirmationService
from app.api.conversations import RedisConversationStore
from app.api.guest import RedisGuestChatLimiter
from app.api.guest_telemetry import OpenTelemetryGuestTelemetry
from app.config import AgentSettings
from app.harness.checkpoint import RedisCheckpointStore
from app.harness.plan import PlanLimits, PlanValidator
from app.harness.supervisor import Supervisor
from app.models.openai_compatible import OpenAICompatibleProvider
from app.models.embeddings import OpenAICompatibleEmbeddingProvider
from app.models.factory import ModelRuntime
from app.memory.extractor import MemoryExtractor
from app.memory.repository import MemoryRepositoryRuntime
from app.memory.service import MemoryService
from app.memory.vector import MemoryOutboxLoop, MemoryOutboxWorker, QdrantMemoryVectorStore
from app.tools.business import build_business_tool_registry
from app.tools.clients import GrpcCatalogClient, GrpcOrderClient
from app.tools.knowledge import PolicyKnowledgeService, QdrantPolicyVectorStore
from app.tasks.graph import InventoryWatchGraph
from app.tasks.mysql_repository import TaskRepositoryRuntime
from app.tasks.service import TaskService
from app.tasks.trace import InventoryWatchTrace
from app.trace.manifests import VersionRegistry
from app.trace.recorder import TracingModelProvider
from app.trace.recorder import TraceRecorderFactory
from app.trace.pricing import PricingCalculator
from app.trace.repository import TraceRepositoryRuntime
from app.trace.safety import TraceSafetyProjector
from app.trace.service import RedisReplayRateLimiter, TraceService


def build_app():
    from app.telemetry import configure_telemetry
    configure_telemetry("bookstore-agent-api")
    settings = AgentSettings()  # type: ignore[call-arg]
    runtime = ModelRuntime(settings)
    raw_model_provider = runtime.chat()
    model_provider = TracingModelProvider(raw_model_provider)
    catalog = GrpcCatalogClient(settings.catalog_rpc_addr)
    orders = GrpcOrderClient(settings.order_rpc_addr)
    embedding_provider = runtime.embeddings()
    policy_store = QdrantPolicyVectorStore(settings.qdrant_url, settings.policy_collection, vector_space=runtime.vector_space)
    memory_repository = MemoryRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    task_repository = TaskRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    trace_repository = TraceRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    memory_store = QdrantMemoryVectorStore(settings.qdrant_url, settings.memory_collection, vector_space=runtime.vector_space)
    memory_service = MemoryService(
        memory_repository,  # type: ignore[arg-type]
        inferred_min_confidence=settings.memory_inferred_min_confidence,
        vector_store=memory_store,
        embeddings=embedding_provider,
        embedding_model=settings.embedding_model,
    )
    memory_extractor = MemoryExtractor(model_provider, settings.agent_model)
    memory_worker = MemoryOutboxWorker(
        memory_repository,  # type: ignore[arg-type]
        memory_store,
        embedding_provider,
        settings.embedding_model,
    )
    memory_loop = MemoryOutboxLoop(memory_worker)
    knowledge = PolicyKnowledgeService(embedding_provider, policy_store, settings.embedding_model)
    tools = build_business_tool_registry(catalog, orders, knowledge)
    versions = runtime.versions
    if tools.version != versions.tool_registry_versions["live-v1"]:
        raise RuntimeError("live Tool Registry does not match its retained version artifact")
    trace_safety = TraceSafetyProjector(
        max_json_bytes=settings.trace_max_json_bytes,
        max_final_answer_bytes=settings.trace_max_final_answer_bytes,
        max_error_bytes=settings.trace_max_error_bytes,
        max_evidence_bytes=settings.trace_max_evidence_bytes,
    )
    trace_factory = TraceRecorderFactory(
        trace_repository,  # type: ignore[arg-type]
        trace_safety,
        versions,
        PricingCalculator(versions.pricing_manifest),
        model_profile=settings.model_profile,
        prompt_set_version=settings.prompt_set_version,
        tool_registry_version=tools.version,
        harness_version=settings.harness_version,
        retention_days=settings.trace_retention_days,
        max_run_bytes=settings.trace_max_run_bytes,
    )
    agents = build_agent_registry()
    limits = PlanLimits(
        max_nodes=settings.agent_max_nodes,
        max_depth=settings.agent_max_depth,
        max_parallel=settings.agent_max_parallel,
        max_tool_calls=settings.agent_max_tool_calls,
    )
    supervisor = Supervisor(model_provider, settings.router_model, PlanValidator(agents, limits), prompt_prefix=runtime.prompt("router"))
    redis = Redis.from_url(settings.redis_addr)
    guest_limiter = (
        RedisGuestChatLimiter(
            redis,
            settings.guest_rate_limit_hmac_key.get_secret_value(),
            settings.trusted_proxy_networks,
            requests_per_hour=settings.guest_chat_requests_per_hour,
            max_concurrent=settings.guest_chat_max_concurrent_per_ip,
        )
        if settings.guest_rate_limit_hmac_key is not None
        else None
    )
    trace_service = TraceService(
        trace_repository,  # type: ignore[arg-type]
        versions,
        RedisReplayRateLimiter(redis, settings.replay_max_per_user_per_hour),
        tool_registry_version=tools.version,
        harness_version=settings.harness_version,
        retention_days=settings.trace_retention_days,
    )
    langgraph_saver = AsyncRedisSaver(redis_client=redis)
    checkpoint_store = RedisCheckpointStore(redis)
    conversations = RedisConversationStore(
        redis,
        confirmation_ttl_seconds=settings.confirmation_binding_ttl_seconds,
    )
    verifier = RepairingVerifier(
        VerificationPipeline(
            DeterministicVerifier(),
            LLMVerifier(model_provider, settings.verifier_model, prompt_prefix=runtime.prompt("verifier")),
        )
    )
    chat_service = ChatService(
        model_provider,
        settings.agent_model,
        supervisor,
        agents,
        tools,
        verifier,
        checkpoint_store,
        langgraph_saver,
        limits,
        conversations,
        memory_service,
        memory_extractor,
        settings.inventory_watch_default_ttl_seconds,
        trace_factory,
        OpenTelemetryGuestTelemetry(),
        prompt_loader=runtime.prompt,
    )
    confirmation_service = ConfirmationService(
        tools,
        conversations,
        checkpoint_store,
        langgraph_saver,
        limits,
        trace_factory,
    )
    inventory_watch_graph = InventoryWatchGraph(langgraph_saver)
    task_service = TaskService(
        task_repository,
        conversations,
        catalog,
        inventory_watch_graph,
        trace=InventoryWatchTrace(trace_factory),
        default_ttl=timedelta(seconds=settings.inventory_watch_default_ttl_seconds),
        max_ttl=timedelta(seconds=settings.inventory_watch_max_ttl_seconds),
    )

    async def validate_trace_manifest_references() -> None:
        versions.assert_database_references(await trace_repository.manifest_references())

    async def validate_vector_collections() -> None:
        await policy_store.ensure_collection(settings.embedding_dimensions)
        await memory_store.ensure_collection(settings.embedding_dimensions)

    return create_app(
        AppDependencies(
            model_provider=model_provider,
            agents=agents,
            tools=tools,
            closeables=(
                memory_loop,
                memory_store,
                memory_repository,
                task_repository,
                trace_repository,
                raw_model_provider,
                embedding_provider,
                policy_store,
                catalog,
                orders,
                redis,
            ),
            startup_hooks=(
                memory_repository.startup,
                task_repository.startup,
                trace_repository.startup,
                validate_trace_manifest_references,
                validate_vector_collections,
                langgraph_saver.asetup,
                memory_loop.startup,
            ),
            token_verifier=AccessTokenVerifier(settings.jwt_public_key_path, settings.jwt_issuer),
            chat_service=chat_service,
            confirmation_service=confirmation_service,
            conversation_store=conversations,
            memory_service=memory_service,
            task_service=task_service,
            trace_service=trace_service,
            guest_limiter=guest_limiter,
            guest_timeout_seconds=settings.guest_chat_timeout_seconds,
        )
    )


try:
    app = build_app()
except Exception:
    # Pydantic/driver startup exceptions can contain the original environment.
    raise RuntimeError("AGENT_STARTUP_FAILED: check configuration and dependency health") from None
