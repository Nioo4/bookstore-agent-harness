from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.auth import AccessTokenVerifier, AuthUser
from app.trace.errors import TraceError
from app.trace.service import ReplayRequest, TraceService


def build_trace_router(service: TraceService, verifier: AccessTokenVerifier) -> APIRouter:
    router = APIRouter(prefix="/api/traces", tags=["traces"])

    @router.get("")
    async def list_traces(request: Request, authorization: str | None = Header(default=None)):
        authenticated = _authenticated(verifier, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        params = request.query_params
        try:
            limit = int(params.get("limit", "20"))
            runs, next_cursor = await service.page_runs(
                authenticated.user_id,
                conversation_id=params.get("conversation_id"),
                kind=params.get("kind"),
                status=params.get("status"),
                cursor=params.get("cursor"),
                limit=limit,
            )
            return {
                "runs": await service.public_summaries(authenticated.user_id, runs),
                "next_cursor": next_cursor,
                "trace_id": _trace_id(),
            }
        except (TraceError, ValueError) as exc:
            return _trace_error(exc)

    @router.get("/{run_id}")
    async def trace_detail(
        run_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        authenticated = _authenticated(verifier, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        params = request.query_params
        try:
            step_limit = int(params.get("step_limit", "100"))
            result = await service.detail(
                authenticated.user_id,
                run_id,
                step_cursor=params.get("step_cursor"),
                step_limit=step_limit,
            )
            return {**result, "trace_id": _trace_id()}
        except (TraceError, ValueError) as exc:
            return _trace_error(exc)

    @router.post("/{run_id}/replays")
    async def create_replay(
        run_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        authenticated = _authenticated(verifier, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        if not authenticated.email_verified:
            return _error(403, "EMAIL_NOT_VERIFIED", "verified email is required", False)
        try:
            body = await request.json()
            parsed = ReplayRequest.model_validate(body)
            run, replayed = await service.create_replay(
                authenticated.user_id,
                run_id,
                parsed,
                idempotency_key=idempotency_key or "",
            )
            return JSONResponse(
                status_code=200 if replayed else 202,
                content={"run": run.public_summary(), "replayed": replayed, "trace_id": _trace_id()},
            )
        except (ValidationError, ValueError):
            return _error(422, "TRACE_REQUEST_INVALID", "invalid replay request", False)
        except TraceError as exc:
            return _trace_error(exc)

    @router.get("/{run_id}/replays")
    async def list_replays(
        run_id: str, request: Request, authorization: str | None = Header(default=None)
    ):
        authenticated = _authenticated(verifier, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        params = request.query_params
        try:
            limit = int(params.get("limit", "20"))
            runs, next_cursor = await service.page_replays(
                authenticated.user_id,
                run_id,
                cursor=params.get("cursor"),
                limit=limit,
            )
            return {
                "runs": await service.public_summaries(authenticated.user_id, runs),
                "next_cursor": next_cursor,
                "trace_id": _trace_id(),
            }
        except (TraceError, ValueError) as exc:
            return _trace_error(exc)

    @router.delete("/{run_id}")
    async def delete_trace(run_id: str, authorization: str | None = Header(default=None)):
        authenticated = _authenticated(verifier, authorization)
        if isinstance(authenticated, JSONResponse):
            return authenticated
        try:
            deleted, scheduled = await service.delete(authenticated.user_id, run_id)
            return {
                "deleted_count": deleted,
                "purge_scheduled": scheduled,
                "trace_id": _trace_id(),
            }
        except TraceError as exc:
            return _trace_error(exc)

    return router


def _authenticated(
    verifier: AccessTokenVerifier, authorization: str | None
) -> AuthUser | JSONResponse:
    try:
        _, user = verifier.verify_authorization(authorization)
    except ValueError:
        return _error(401, "AUTH_REQUIRED", "invalid access token", False)
    if user is None:
        return _error(401, "AUTH_REQUIRED", "authentication is required", False)
    return user


def _trace_error(exc: Exception) -> JSONResponse:
    if not isinstance(exc, TraceError):
        return _error(422, "TRACE_REQUEST_INVALID", "invalid trace request", False)
    statuses = {
        "TRACE_NOT_FOUND": 404,
        "EMAIL_NOT_VERIFIED": 403,
        "TRACE_NOT_REPLAYABLE": 409,
        "TRACE_NOT_DELETABLE": 409,
        "REPLAY_ALREADY_RUNNING": 409,
        "IDEMPOTENCY_CONFLICT": 409,
        "TRACE_REQUEST_INVALID": 422,
        "MODEL_PROFILE_NOT_ALLOWED": 422,
        "PROMPT_VERSION_NOT_FOUND": 422,
        "RATE_LIMITED": 429,
        "TRACE_DEPENDENCY_UNAVAILABLE": 503,
    }
    return _error(statuses.get(exc.code, 503), exc.code, str(exc), exc.retryable)


def _error(status_code: int, code: str, message: str, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "trace_id": _trace_id(),
            }
        },
    )


def _trace_id() -> str:
    return str(uuid.uuid4())
