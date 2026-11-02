from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from app.trace.safety import TraceSafetyProjector


CONTEXT_TTL_SECONDS = 24 * 60 * 60
CONTEXT_MAX_TURNS = 10
CONTEXT_MAX_CHARACTERS = 8_000


class DisplayedBook(BaseModel):
    """A safe reference to a book that was actually shown to the user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: int = Field(gt=0)
    isbn: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: str = Field(min_length=1)
    answer: str = Field(min_length=1)


class ConversationContext(BaseModel):
    """Bounded, non-authoritative context for one authenticated browser page."""

    model_config = ConfigDict(extra="forbid")

    turns: list[ConversationTurn] = Field(default_factory=list)
    active_request: str = ""
    constraints: dict[str, Any] = Field(default_factory=dict)
    displayed_books: list[DisplayedBook] = Field(default_factory=list, max_length=50)
    selected_book: DisplayedBook | None = None


class ConversationContextLease(BaseModel):
    """A generation lease prevents an older running request from overwriting state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generation: int = Field(ge=1)
    context: ConversationContext


class ConversationNotFound(Exception):
    pass


class PendingConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    user_id: int = Field(gt=0)
    action_type: str = "CREATE_ORDER"
    risk: str = "COMMIT"


class ConversationStore(Protocol):
    async def bind(self, conversation_id: str, user_id: int | None) -> None: ...
    async def assert_owner(self, conversation_id: str, user_id: int) -> None: ...
    async def register_confirmation(self, raw_token: str, pending: PendingConfirmation) -> None: ...
    async def get_confirmation(self, raw_token: str, conversation_id: str, user_id: int) -> PendingConfirmation: ...
    async def append_system_event(self, conversation_id: str, event: dict[str, Any]) -> None: ...
    async def append_trace_event(self, run_id: str, event: dict[str, Any]) -> None: ...
    async def begin_context(self, conversation_id: str, user_id: int) -> ConversationContextLease: ...
    async def commit_context(
        self,
        conversation_id: str,
        user_id: int,
        generation: int,
        context: ConversationContext,
    ) -> bool: ...


def confirmation_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class InMemoryConversationStore:
    def __init__(self) -> None:
        self.owners: dict[str, int] = {}
        self.confirmations: dict[str, PendingConfirmation] = {}
        self.system_events: dict[str, list[dict[str, Any]]] = {}
        self.trace_events: dict[str, list[dict[str, Any]]] = {}
        self.contexts: dict[str, ConversationContext] = {}
        self.context_generations: dict[str, int] = {}

    async def bind(self, conversation_id: str, user_id: int | None) -> None:
        if user_id is None:
            if conversation_id in self.owners:
                raise ConversationNotFound
            return
        owner = self.owners.setdefault(conversation_id, user_id)
        if owner != user_id:
            raise ConversationNotFound

    async def assert_owner(self, conversation_id: str, user_id: int) -> None:
        if self.owners.get(conversation_id) != user_id:
            raise ConversationNotFound

    async def register_confirmation(self, raw_token: str, pending: PendingConfirmation) -> None:
        self.confirmations[confirmation_digest(raw_token)] = pending.model_copy(deep=True)

    async def get_confirmation(self, raw_token: str, conversation_id: str, user_id: int) -> PendingConfirmation:
        pending = self.confirmations.get(confirmation_digest(raw_token))
        if pending is None or pending.conversation_id != conversation_id or pending.user_id != user_id:
            raise ConversationNotFound
        return pending.model_copy(deep=True)

    async def append_system_event(self, conversation_id: str, event: dict[str, Any]) -> None:
        self.system_events.setdefault(conversation_id, []).append(dict(event))

    async def append_trace_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.trace_events.setdefault(run_id, []).append(dict(event))

    async def begin_context(self, conversation_id: str, user_id: int) -> ConversationContextLease:
        await self.assert_owner(conversation_id, user_id)
        generation = self.context_generations.get(conversation_id, 0) + 1
        self.context_generations[conversation_id] = generation
        return ConversationContextLease(
            generation=generation,
            context=self.contexts.get(conversation_id, ConversationContext()).model_copy(deep=True),
        )

    async def commit_context(
        self,
        conversation_id: str,
        user_id: int,
        generation: int,
        context: ConversationContext,
    ) -> bool:
        await self.assert_owner(conversation_id, user_id)
        if self.context_generations.get(conversation_id) != generation:
            return False
        self.contexts[conversation_id] = _bounded_context(context)
        return True


class RedisConversationStore:
    def __init__(self, redis: Redis, *, confirmation_ttl_seconds: int) -> None:
        if confirmation_ttl_seconds < 24 * 60 * 60:
            raise ValueError("confirmation binding TTL must be at least 24 hours")
        self.redis = redis
        self.confirmation_ttl_seconds = confirmation_ttl_seconds

    async def bind(self, conversation_id: str, user_id: int | None) -> None:
        key = f"agent:conversation:{conversation_id}:owner"
        if user_id is None:
            if await self.redis.exists(key):
                raise ConversationNotFound
            return
        created = await self.redis.set(key, str(user_id), nx=True)
        if not created:
            await self.assert_owner(conversation_id, user_id)

    async def assert_owner(self, conversation_id: str, user_id: int) -> None:
        value = await self.redis.get(f"agent:conversation:{conversation_id}:owner")
        if value is None or int(value) != user_id:
            raise ConversationNotFound

    async def register_confirmation(self, raw_token: str, pending: PendingConfirmation) -> None:
        key = f"agent:confirmation:{confirmation_digest(raw_token)}"
        await self.redis.set(key, pending.model_dump_json(), ex=self.confirmation_ttl_seconds)

    async def get_confirmation(self, raw_token: str, conversation_id: str, user_id: int) -> PendingConfirmation:
        value = await self.redis.get(f"agent:confirmation:{confirmation_digest(raw_token)}")
        if value is None:
            raise ConversationNotFound
        pending = PendingConfirmation.model_validate_json(value)
        if pending.conversation_id != conversation_id or pending.user_id != user_id:
            raise ConversationNotFound
        return pending

    async def append_system_event(self, conversation_id: str, event: dict[str, Any]) -> None:
        envelope = {"recorded_at": datetime.now(UTC).isoformat(), **event}
        await self.redis.rpush(f"agent:conversation:{conversation_id}:events", _safe_json(envelope))

    async def append_trace_event(self, run_id: str, event: dict[str, Any]) -> None:
        envelope = {"recorded_at": datetime.now(UTC).isoformat(), **event}
        await self.redis.rpush(f"agent:trace:{run_id}:events", _safe_json(envelope))

    async def begin_context(self, conversation_id: str, user_id: int) -> ConversationContextLease:
        await self.assert_owner(conversation_id, user_id)
        sequence_key = f"agent:conversation:{conversation_id}:context-sequence"
        context_key = f"agent:conversation:{conversation_id}:context"
        generation, raw_context = await self.redis.eval(
            """
            local generation = redis.call('INCR', KEYS[1])
            redis.call('EXPIRE', KEYS[1], ARGV[1])
            local context = redis.call('GET', KEYS[2])
            if context then redis.call('EXPIRE', KEYS[2], ARGV[1]) end
            return { generation, context }
            """,
            2,
            sequence_key,
            context_key,
            CONTEXT_TTL_SECONDS,
        )
        context = ConversationContext()
        if raw_context is not None:
            context = ConversationContext.model_validate_json(_as_text(raw_context))
        return ConversationContextLease(generation=int(generation), context=context)

    async def commit_context(
        self,
        conversation_id: str,
        user_id: int,
        generation: int,
        context: ConversationContext,
    ) -> bool:
        await self.assert_owner(conversation_id, user_id)
        sequence_key = f"agent:conversation:{conversation_id}:context-sequence"
        context_key = f"agent:conversation:{conversation_id}:context"
        committed = await self.redis.eval(
            """
            local current = redis.call('GET', KEYS[1])
            if not current or tonumber(current) ~= tonumber(ARGV[1]) then return 0 end
            redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
            return 1
            """,
            2,
            sequence_key,
            context_key,
            generation,
            _bounded_context(context).model_dump_json(),
            CONTEXT_TTL_SECONDS,
        )
        return bool(int(committed))


def _safe_json(value: dict[str, Any]) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded_context(context: ConversationContext) -> ConversationContext:
    """Redact and evict complete old turns before writing a short-lived context."""
    projector = TraceSafetyProjector()
    turns = [
        ConversationTurn(
            request=projector.text(turn.request).value,
            answer=projector.text(turn.answer).value,
        )
        for turn in context.turns
    ]
    while len(turns) > CONTEXT_MAX_TURNS or sum(len(turn.request) + len(turn.answer) for turn in turns) > CONTEXT_MAX_CHARACTERS:
        turns.pop(0)
    safe_books = [
        DisplayedBook.model_validate(projector.json(book.model_dump(mode="json")).value)
        for book in context.displayed_books
    ]
    safe_selected = (
        DisplayedBook.model_validate(projector.json(context.selected_book.model_dump(mode="json")).value)
        if context.selected_book is not None
        else None
    )
    safe_constraints = projector.json(context.constraints).value
    return context.model_copy(
        update={
            "turns": turns,
            "active_request": projector.text(context.active_request).value,
            "constraints": safe_constraints,
            "displayed_books": safe_books,
            "selected_book": safe_selected,
        },
        deep=True,
    )


def _as_text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value
