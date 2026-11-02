from __future__ import annotations

"""Production Agent API wiring for the v2 single-agent runtime."""

from datetime import timedelta
from pathlib import Path

from redis.asyncio import Redis

from app.api.app import AppDependencies, create_app
from app.api.auth import AccessTokenVerifier
from app.api.chat import ChatService
from app.api.confirmation import ConfirmationService
from app.api.conversations import RedisConversationStore
from app.api.guest import RedisGuestChatLimiter
from app.api.guest_telemetry import OpenTelemetryGuestTelemetry
from app.config import AgentAPISettings
from app.harness.single_agent import SingleAgentLimits, SingleAgentRuntime
from app.memory.repository import MemoryRepositoryRuntime
from app.memory.service import MemoryService
from app.models.factory import ModelRuntime
from app.rag.contracts import RagManifest
from app.rag.qdrant import QdrantRagStore
from app.rag.service import RagSearchService
from app.tasks.v2_repository import TaskRepositoryRuntime
from app.tasks.service import TaskService
from app.tools.business import build_business_tool_registry
from app.tools.clients import GrpcCatalogClient, GrpcOrderClient
from app.trace.pricing import PricingCalculator
from app.trace.recorder import TraceRecorderFactory, TracingModelProvider
from app.trace.repository import TraceRepositoryRuntime
from app.trace.safety import TraceSafetyProjector
from app.trace.service import TraceService


def build_app():
    from app.telemetry import configure_telemetry

    configure_telemetry("bookstore-agent-api")
    settings = AgentAPISettings()  # type: ignore[call-arg]
    manifest_path = Path(__file__).resolve().parents[1] / settings.rag_index_manifest_path
    rag_manifest = RagManifest.read(manifest_path)
    rag_manifest.verify_hash()
    rag_manifest.assert_compatible(model=settings.embedding_model, dimensions=settings.embedding_dimensions)
    runtime = ModelRuntime(settings)
    raw_model_provider = runtime.chat()
    model_provider = TracingModelProvider(raw_model_provider)
    embedding_provider = runtime.embeddings()

    # v2 has one consolidated Go business service; no etcd or per-RPC process
    # is used by the Agent runtime.
    catalog = GrpcCatalogClient(settings.business_grpc_addr)
    orders = GrpcOrderClient(settings.business_grpc_addr)
    policy_store = QdrantRagStore(
        settings.qdrant_url,
        alias=settings.policy_collection_alias,
        dimensions=settings.embedding_dimensions,
    )
    book_store = QdrantRagStore(
        settings.qdrant_url,
        alias=settings.book_collection_alias,
        dimensions=settings.embedding_dimensions,
    )
    rag = RagSearchService(
        embedding_provider,
        policy_store,
        book_store,
        embedding_model=settings.embedding_model,
        policy_threshold=rag_manifest.thresholds["POLICY"],
        book_threshold=rag_manifest.thresholds["BOOK_GUIDE"],
    )
    tools = build_business_tool_registry(catalog, orders, rag, rag)
    versions = runtime.versions
    if tools.version != versions.tool_registry_versions["live-v2"]:
        raise RuntimeError("live Tool Registry does not match its retained version artifact")

    memory_repository = MemoryRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    task_repository = TaskRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    trace_repository = TraceRepositoryRuntime(settings.agent_mysql_dsn.get_secret_value())
    memory_service = MemoryService(memory_repository, v2_mode=True)
    redis = Redis.from_url(settings.redis_addr)
    conversations = RedisConversationStore(
        redis,
        confirmation_ttl_seconds=settings.confirmation_binding_ttl_seconds,
    )
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
    limits = SingleAgentLimits(
        max_rounds=settings.agent_max_rounds,
        max_tool_calls=settings.agent_max_tool_calls,
        max_parallel_reads=settings.agent_max_parallel_reads,
        timeout_seconds=float(settings.agent_request_timeout_seconds),
        rag_max_context_tokens=settings.rag_max_context_tokens,
    )
    single_agent = SingleAgentRuntime(
        model_provider,
        settings.agent_model,
        tools,
        limits=limits,
        system_prompt=runtime.prompt("single_agent"),
    )

    chat_service = ChatService(
        model_provider,
        settings.agent_model,
        tools,
        conversations,
        memory_service=memory_service,
        inventory_watch_default_ttl_seconds=settings.inventory_watch_default_ttl_seconds,
        trace_factory=trace_factory,
        guest_telemetry=OpenTelemetryGuestTelemetry(),
        single_agent_runtime=single_agent,
    )
    confirmation_service = ConfirmationService(tools, conversations, trace_factory=trace_factory)
    task_service = TaskService(
        task_repository,
        conversations,
        catalog,
        default_ttl=timedelta(seconds=settings.inventory_watch_default_ttl_seconds),
        max_ttl=timedelta(seconds=settings.inventory_watch_max_ttl_seconds),
    )
    trace_service = TraceService(
        trace_repository,  # type: ignore[arg-type]
        versions,
        None,
        tool_registry_version=tools.version,
        harness_version=settings.harness_version,
        retention_days=settings.trace_retention_days,
    )

    async def validate_trace_manifest_references() -> None:
        versions.assert_database_references(await trace_repository.manifest_references())

    async def validate_rag_manifest_and_aliases() -> None:
        await policy_store.assert_alias_compatible(rag_manifest.corpora["POLICY"].collection)
        await book_store.assert_alias_compatible(rag_manifest.corpora["BOOK_GUIDE"].collection)

    return create_app(
        AppDependencies(
            model_provider=model_provider,
            tools=tools,
            closeables=(
                memory_repository,
                task_repository,
                trace_repository,
                policy_store,
                book_store,
                raw_model_provider,
                embedding_provider,
                catalog,
                orders,
                redis,
            ),
            startup_hooks=(
                memory_repository.startup,
                task_repository.startup,
                trace_repository.startup,
                validate_trace_manifest_references,
                validate_rag_manifest_and_aliases,
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
    raise RuntimeError("AGENT_STARTUP_FAILED: check configuration and dependency health") from None
