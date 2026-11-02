from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Header, Query, Request
from pydantic import ValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.auth import AccessTokenVerifier
from app.api.chat import ChatEvent, ChatMessageRequest, ChatService
from app.api.confirmation import ConfirmationDecision, ConfirmationService
from app.api.conversations import ConversationNotFound, ConversationStore
from app.api.guest import GuestChatLimiter, GuestLimitError
from app.models.provider import ModelProvider
from app.memory.errors import MemoryDependencyUnavailable, MemoryNotFound
from app.memory.service import MemoryService
from app.memory.types import MemoryType
from app.tools.registry import ToolRegistry
from app.tasks.errors import TaskError
from app.tasks.service import TaskService
from app.tasks.types import InventoryWatchRequest, TaskStatus
from app.trace.service import TraceService
from app.telemetry import SafeHTTPMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response


@dataclass(frozen=True)
class AppDependencies:
    model_provider: ModelProvider
    tools: ToolRegistry
    # Compatibility-only value accepted by old fixtures; v2 production wiring
    # never reads it and registers one SingleAgentRuntime instead.
    agents: Any | None = None
    closeables: tuple[Any, ...] = ()
    startup_hooks: tuple[Any, ...] = ()
    token_verifier: AccessTokenVerifier | None = None
    chat_service: ChatService | None = None
    confirmation_service: ConfirmationService | None = None
    conversation_store: ConversationStore | None = None
    memory_service: MemoryService | None = None
    task_service: TaskService | None = None
    trace_service: TraceService | None = None
    guest_limiter: GuestChatLimiter | None = None
    guest_timeout_seconds: int = 120


def create_app(dependencies: AppDependencies) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for hook in dependencies.startup_hooks:
            await hook()
        yield
        for resource in dependencies.closeables:
            close = getattr(resource, "aclose", None)
            if close is not None:
                await close()

    app = FastAPI(title="Bookstore Agent Service", version="0.1.0", lifespan=lifespan)
    app.state.dependencies = dependencies
    app.add_middleware(SafeHTTPMiddleware)
    if dependencies.trace_service is not None and dependencies.token_verifier is not None:
        from app.api.traces import build_trace_router

        app.include_router(
            build_trace_router(
                dependencies.trace_service,
                dependencies.token_verifier,
            )
        )

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "bookstore-agent"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.get("/internal/tools", tags=["internal"])
    async def tool_manifests() -> dict[str, list[dict[str, object]]]:
        return {"tools": dependencies.tools.manifests()}

    @app.post("/api/chat/conversations/{conversation_id}/messages", tags=["chat"])
    async def chat_message(
        conversation_id: str,
        body: ChatMessageRequest,
        http_request: Request,
        authorization: str | None = Header(default=None),
    ):
        if dependencies.chat_service is None or dependencies.token_verifier is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "DEPENDENCY_UNAVAILABLE",
                        "message": "chat service is unavailable",
                        "retryable": True,
                        "trace_id": "",
                    }
                },
            )
        if not conversation_id or len(conversation_id) > 128:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "invalid conversation id",
                        "retryable": False,
                        "trace_id": "",
                    }
                },
            )
        try:
            access_token, user = dependencies.token_verifier.verify_authorization(authorization)
        except ValueError:
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "AUTH_REQUIRED",
                        "message": "invalid access token",
                        "retryable": False,
                        "trace_id": "",
                    }
                },
            )
        if dependencies.conversation_store is not None and user is not None:
            try:
                await dependencies.conversation_store.bind(conversation_id, user.user_id)
            except ConversationNotFound:
                return _error(404, "CONVERSATION_NOT_FOUND", "conversation not found", False)

        guest_lease = None
        if user is None:
            if dependencies.guest_limiter is None:
                return _error(
                    503,
                    "DEPENDENCY_UNAVAILABLE",
                    "guest rate limiter is unavailable",
                    True,
                )
            try:
                guest_lease = await dependencies.guest_limiter.acquire(
                    direct_ip=http_request.client.host if http_request.client is not None else "",
                    forwarded_for=http_request.headers.get("x-forwarded-for"),
                )
            except GuestLimitError as exc:
                return _error(exc.status_code, exc.code, str(exc), exc.retryable)

        recorder = None
        begin_trace = getattr(dependencies.chat_service, "begin_trace", None)
        if begin_trace is not None:
            try:
                recorder = await begin_trace(conversation_id, body, user)
            except Exception as exc:
                from app.trace.errors import TraceDependencyUnavailable

                if isinstance(exc, TraceDependencyUnavailable):
                    return _error(
                        503,
                        "TRACE_DEPENDENCY_UNAVAILABLE",
                        "initial trace run could not be created",
                        True,
                    )
                raise

        async def events():
            stream_kwargs = {"access_token": access_token, "user": user}
            if recorder is not None:
                stream_kwargs["recorder"] = recorder
            try:
                async with asyncio.timeout(dependencies.guest_timeout_seconds if user is None else None):
                    async for event in dependencies.chat_service.stream(  # type: ignore[arg-type]
                        conversation_id, body, **stream_kwargs
                    ):
                        yield event.encode_sse()
            except TimeoutError:
                yield ChatEvent(
                    event="run.failed",
                    data={
                        "code": "REQUEST_TIMEOUT",
                        "retryable": True,
                        "trace_persisted": False,
                    },
                ).encode_sse()
            finally:
                if guest_lease is not None:
                    await guest_lease.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chat/conversations/{conversation_id}/confirmations", tags=["chat"])
    async def decide_confirmation(
        conversation_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        if dependencies.confirmation_service is None or dependencies.token_verifier is None:
            return _error(503, "DEPENDENCY_UNAVAILABLE", "confirmation service is unavailable", True)
        if not conversation_id or len(conversation_id) > 128:
            return _error(400, "INVALID_REQUEST", "invalid conversation id", False)
        try:
            access_token, user = dependencies.token_verifier.verify_authorization(authorization)
        except ValueError:
            return _error(401, "AUTH_REQUIRED", "invalid access token", False)
        if access_token is None or user is None:
            return _error(401, "AUTH_REQUIRED", "authentication is required", False)
        try:
            body = await request.json()
        except Exception:
            return _error(400, "INVALID_REQUEST", "invalid JSON request body", False)
        if not isinstance(body, dict):
            return _error(400, "INVALID_REQUEST", "request body must be an object", False)
        if body.get("decision") not in {"APPROVE", "REJECT"}:
            return _error(422, "INVALID_CONFIRMATION_DECISION", "decision must be APPROVE or REJECT", False)
        try:
            parsed = ConfirmationDecision.model_validate(body)
        except ValidationError:
            return _error(400, "INVALID_REQUEST", "invalid confirmation request", False)
        try:
            result = await dependencies.confirmation_service.decide(
                conversation_id,
                parsed,
                access_token=access_token,
                user=user,
            )
            return JSONResponse(status_code=200, content=result)
        except ConversationNotFound:
            return _error(404, "CONFIRMATION_NOT_FOUND", "confirmation not found", False)
        except Exception as exc:
            from app.tools.errors import ToolError
            if not isinstance(exc, ToolError):
                return _error(503, "DEPENDENCY_UNAVAILABLE", "confirmation dependency is unavailable", True)
            status_by_code = {
                "CONFIRMATION_NOT_FOUND": 404,
                "CONFIRMATION_EXPIRED": 410,
                "CONFIRMATION_ALREADY_DECIDED": 409,
                "INVENTORY_INSUFFICIENT": 409,
                "PRICE_CHANGED": 409,
                "BOOK_INACTIVE": 409,
                "IDEMPOTENCY_CONFLICT": 409,
                "AUTH_REQUIRED": 401,
                "EMAIL_NOT_VERIFIED": 403,
            }
            return _error(status_by_code.get(exc.code, 503), exc.code, str(exc), exc.retryable)

    @app.get("/api/memories", tags=["memory"])
    async def list_memories(
        authorization: str | None = Header(default=None),
        memory_type: str | None = Query(default=None, alias="type"),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        authenticated = _memory_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            parsed_type = MemoryType(memory_type) if memory_type is not None else None
        except ValueError:
            return _error(422, "MEMORY_VALUE_INVALID", "unsupported memory type", False)
        try:
            memories = await dependencies.memory_service.list(authenticated.user_id, parsed_type, limit)  # type: ignore[union-attr]
            return JSONResponse(
                status_code=200,
                content={"memories": [memory.model_dump(mode="json") for memory in memories]},
            )
        except MemoryDependencyUnavailable:
            return _error(503, "MEMORY_DEPENDENCY_UNAVAILABLE", "memory dependency is unavailable", True)

    @app.post("/api/memories/preferences", tags=["memory"])
    async def upsert_preference(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        authenticated = _memory_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            body = await request.json()
        except Exception:
            return _error(400, "INVALID_REQUEST", "invalid JSON request body", False)
        if not isinstance(body, dict) or set(body) != {"key", "value"}:
            return _error(422, "MEMORY_VALUE_INVALID", "preference body must contain key and value", False)
        key, value = body.get("key"), body.get("value")
        if not isinstance(key, str) or not isinstance(value, str):
            return _error(422, "MEMORY_VALUE_INVALID", "preference key and value must be strings", False)
        try:
            record, created = await dependencies.memory_service.upsert_preference(  # type: ignore[union-attr]
                authenticated.user_id, key, value
            )
            stored_value = record.value.get("value") if isinstance(record.value, dict) else record.value
            return JSONResponse(
                status_code=200,
                content={
                    "memory_id": record.memory_id,
                    "key": record.memory_key,
                    "value": stored_value,
                    "created": created,
                    "updated_at": record.updated_at.isoformat(),
                },
            )
        except Exception as exc:
            from app.memory.errors import MemoryKeyUnsupported, MemoryValueInvalid
            if isinstance(exc, (MemoryKeyUnsupported, MemoryValueInvalid)):
                return _error(422, "MEMORY_VALUE_INVALID", str(exc), False)
            if isinstance(exc, MemoryDependencyUnavailable):
                return _error(503, "MEMORY_DEPENDENCY_UNAVAILABLE", "memory dependency is unavailable", True)
            raise

    @app.delete("/api/memories/{memory_id}", tags=["memory"])
    async def delete_memory(memory_id: str, authorization: str | None = Header(default=None)):
        authenticated = _memory_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            result = await dependencies.memory_service.delete_one(authenticated.user_id, memory_id)  # type: ignore[union-attr]
            return JSONResponse(status_code=200, content=result)
        except MemoryNotFound:
            return _error(404, "MEMORY_NOT_FOUND", "memory not found", False)
        except MemoryDependencyUnavailable:
            return _error(503, "MEMORY_DEPENDENCY_UNAVAILABLE", "memory dependency is unavailable", True)

    @app.delete("/api/memories", tags=["memory"])
    async def delete_all_memories(authorization: str | None = Header(default=None)):
        authenticated = _memory_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            result = await dependencies.memory_service.delete_all(authenticated.user_id)  # type: ignore[union-attr]
            return JSONResponse(status_code=200, content=result)
        except MemoryDependencyUnavailable:
            return _error(503, "MEMORY_DEPENDENCY_UNAVAILABLE", "memory dependency is unavailable", True)

    @app.post("/api/tasks/inventory-watches", tags=["tasks"])
    async def create_inventory_watch(
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        access_token, user = authenticated
        try:
            body = await request.json()
            parsed = InventoryWatchRequest.model_validate(body)
        except Exception:
            return _error(422, "INVALID_TASK_REQUEST", "invalid inventory watch request", False)
        try:
            task, replayed = await dependencies.task_service.create(  # type: ignore[union-attr]
                parsed,
                idempotency_key=idempotency_key or "",
                access_token=access_token,
                user=user,
            )
            return JSONResponse(
                status_code=200 if replayed else 201,
                content={"task": task.public(), "replayed": replayed, "trace_id": _trace_id()},
            )
        except TaskError as exc:
            return _task_error(exc)

    @app.get("/api/tasks", tags=["tasks"])
    async def list_tasks(request: Request, authorization: str | None = Header(default=None)):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        _, user = authenticated
        params = request.query_params
        try:
            task_type = params.get("type")
            if task_type is not None and task_type != "INVENTORY_WATCH":
                raise TaskError("INVALID_TASK_REQUEST", "unsupported task type")
            status = TaskStatus(params["status"]) if params.get("status") else None
            limit = int(params.get("limit", "20"))
            tasks, next_cursor = await dependencies.task_service.page_tasks(  # type: ignore[union-attr]
                user.user_id, task_type, status, params.get("cursor"), limit
            )
            return {"tasks": [task.public() for task in tasks], "next_cursor": next_cursor, "trace_id": _trace_id()}
        except TaskError as exc:
            return _task_error(exc)
        except ValueError:
            return _error(422, "INVALID_TASK_REQUEST", "invalid task query", False)

    @app.get("/api/tasks/{task_id}", tags=["tasks"])
    async def get_task(task_id: str, authorization: str | None = Header(default=None)):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        _, user = authenticated
        try:
            task = await dependencies.task_service.get_task(user.user_id, task_id)  # type: ignore[union-attr]
            return {"task": task.public(), "trace_id": _trace_id()}
        except TaskError as exc:
            return _task_error(exc)

    @app.post("/api/tasks/{task_id}/cancel", tags=["tasks"])
    async def cancel_task(task_id: str, authorization: str | None = Header(default=None)):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        _, user = authenticated
        try:
            task, replayed = await dependencies.task_service.cancel(user.user_id, task_id)  # type: ignore[union-attr]
            return {"task": task.public(), "replayed": replayed, "trace_id": _trace_id()}
        except TaskError as exc:
            return _task_error(exc)

    @app.get("/api/notifications", tags=["notifications"])
    async def list_notifications(request: Request, authorization: str | None = Header(default=None)):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        _, user = authenticated
        params = request.query_params
        try:
            unread_text = params.get("unread_only", "false").casefold()
            if unread_text not in {"true", "false"}:
                raise ValueError
            limit = int(params.get("limit", "20"))
            items, next_cursor = await dependencies.task_service.page_notifications(  # type: ignore[union-attr]
                user.user_id, unread_text == "true", params.get("cursor"), limit
            )
            return {
                "notifications": [item.public() for item in items],
                "next_cursor": next_cursor,
                "trace_id": _trace_id(),
            }
        except TaskError as exc:
            return _task_error(exc)
        except ValueError:
            return _error(422, "INVALID_TASK_REQUEST", "invalid notification query", False)

    @app.post("/api/notifications/{notification_id}/read", tags=["notifications"])
    async def read_notification(notification_id: str, authorization: str | None = Header(default=None)):
        authenticated = _task_user(dependencies, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        _, user = authenticated
        try:
            item, replayed = await dependencies.task_service.read_notification(  # type: ignore[union-attr]
                user.user_id, notification_id
            )
            return {"notification": item.public(), "replayed": replayed, "trace_id": _trace_id()}
        except TaskError as exc:
            return _task_error(exc)

    return app


def _error(status_code: int, code: str, message: str, retryable: bool, trace_id: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "retryable": retryable, "trace_id": trace_id}},
    )


def _memory_user(dependencies: AppDependencies, authorization: str | None):
    if dependencies.memory_service is None or dependencies.token_verifier is None:
        return _error(503, "MEMORY_DEPENDENCY_UNAVAILABLE", "memory service is unavailable", True)
    try:
        _, user = dependencies.token_verifier.verify_authorization(authorization)
    except ValueError:
        return _error(401, "AUTH_REQUIRED", "invalid access token", False)
    if user is None:
        return _error(401, "AUTH_REQUIRED", "authentication is required", False)
    return user


def _task_user(dependencies: AppDependencies, authorization: str | None):
    if dependencies.task_service is None or dependencies.token_verifier is None:
        return _error(503, "TASK_DEPENDENCY_UNAVAILABLE", "task service is unavailable", True)
    try:
        access_token, user = dependencies.token_verifier.verify_authorization(authorization)
    except ValueError:
        return _error(401, "AUTH_REQUIRED", "invalid access token", False)
    if access_token is None or user is None:
        return _error(401, "AUTH_REQUIRED", "authentication is required", False)
    return access_token, user


def _task_error(exc: TaskError) -> JSONResponse:
    statuses = {
        "INVALID_TASK_REQUEST": 422,
        "AUTH_REQUIRED": 401,
        "EMAIL_NOT_VERIFIED": 403,
        "CONVERSATION_NOT_FOUND": 404,
        "BOOK_NOT_FOUND": 404,
        "TASK_NOT_FOUND": 404,
        "NOTIFICATION_NOT_FOUND": 404,
        "IDEMPOTENCY_CONFLICT": 409,
        "INVENTORY_WATCH_NOT_NEEDED": 409,
        "INVENTORY_WATCH_ALREADY_ACTIVE": 409,
        "TASK_NOT_CANCELLABLE": 409,
        "BOOK_INACTIVE": 409,
        "TASK_DEPENDENCY_UNAVAILABLE": 503,
        "TRACE_DEPENDENCY_UNAVAILABLE": 503,
    }
    return _error(statuses.get(exc.code, 503), exc.code, str(exc), exc.retryable)


def _trace_id() -> str:
    import uuid
    return str(uuid.uuid4())
