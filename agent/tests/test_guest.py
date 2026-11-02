from __future__ import annotations

import hashlib
import hmac

import pytest

from app.api.guest import GuestLimitError, RedisGuestChatLimiter


class ScriptedRedis:
    def __init__(self, result=0, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.eval_calls = []
        self.zrem_calls = []

    async def eval(self, *args):
        self.eval_calls.append(args)
        if self.error is not None:
            raise self.error
        return self.result

    async def zrem(self, *args):
        self.zrem_calls.append(args)
        return 1


@pytest.mark.asyncio
async def test_guest_limiter_ignores_spoofed_forwarded_header_from_untrusted_client() -> None:
    redis = ScriptedRedis()
    key = "k" * 32
    limiter = RedisGuestChatLimiter(redis, key, ("10.0.0.0/8",))  # type: ignore[arg-type]

    lease = await limiter.acquire(direct_ip="203.0.113.9", forwarded_for="198.51.100.7")
    call = redis.eval_calls[0]
    expected = hmac.new(key.encode(), b"203.0.113.9", hashlib.sha256).hexdigest()
    assert expected in call[3]
    assert "203.0.113.9" not in repr(call)
    assert "198.51.100.7" not in repr(call)

    await lease.release()
    assert len(redis.zrem_calls) == 1


@pytest.mark.asyncio
async def test_guest_limiter_uses_single_overwritten_address_only_from_trusted_proxy() -> None:
    redis = ScriptedRedis()
    key = "z" * 32
    limiter = RedisGuestChatLimiter(redis, key, ("10.0.0.0/8",))  # type: ignore[arg-type]

    await limiter.acquire(direct_ip="10.1.2.3", forwarded_for="198.51.100.11")
    expected = hmac.new(key.encode(), b"198.51.100.11", hashlib.sha256).hexdigest()
    assert expected in redis.eval_calls[0][3]

    with pytest.raises(GuestLimitError) as rejected:
        await limiter.acquire(direct_ip="10.1.2.3", forwarded_for="198.51.100.11, 10.1.2.3")
    assert rejected.value.code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "code", "status"),
    [(1, "RATE_LIMITED", 429), (2, "RATE_LIMITED", 429), (9, "DEPENDENCY_UNAVAILABLE", 503)],
)
async def test_guest_limiter_maps_atomic_script_results(result: int, code: str, status: int) -> None:
    limiter = RedisGuestChatLimiter(ScriptedRedis(result), "x" * 32, ())  # type: ignore[arg-type]
    with pytest.raises(GuestLimitError) as raised:
        await limiter.acquire(direct_ip="192.0.2.1", forwarded_for=None)
    assert (raised.value.code, raised.value.status_code) == (code, status)


@pytest.mark.asyncio
async def test_guest_limiter_fails_closed_when_redis_is_unavailable() -> None:
    limiter = RedisGuestChatLimiter(
        ScriptedRedis(error=ConnectionError("secret infrastructure detail")),
        "x" * 32,
        (),
    )  # type: ignore[arg-type]
    with pytest.raises(GuestLimitError) as raised:
        await limiter.acquire(direct_ip="192.0.2.1", forwarded_for=None)
    assert raised.value.code == "DEPENDENCY_UNAVAILABLE"
    assert "secret infrastructure detail" not in str(raised.value)
