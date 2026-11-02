from __future__ import annotations

import os

import pytest
from redis.asyncio import Redis

from app.api.guest import GuestLimitError, RedisGuestChatLimiter


@pytest.mark.asyncio
async def test_real_redis_guest_rate_and_concurrency_limits_are_atomic_and_expiring() -> None:
    url = os.getenv("GUEST_TEST_REDIS_URL")
    if not url:
        pytest.skip("GUEST_TEST_REDIS_URL is required")
    redis = Redis.from_url(url, decode_responses=True)
    await redis.flushdb()
    try:
        limiter = RedisGuestChatLimiter(
            redis,
            "integration-test-hmac-key-32-bytes-minimum",
            ("127.0.0.1/32",),
            requests_per_hour=10,
            max_concurrent=2,
        )
        first = await limiter.acquire(direct_ip="127.0.0.1", forwarded_for="198.51.100.23")
        second = await limiter.acquire(direct_ip="127.0.0.1", forwarded_for="198.51.100.23")
        with pytest.raises(GuestLimitError) as concurrent:
            await limiter.acquire(direct_ip="127.0.0.1", forwarded_for="198.51.100.23")
        assert concurrent.value.code == "RATE_LIMITED"
        keys = [str(key) for key in await redis.keys("guest:chat:*")]
        assert keys and all("198.51.100.23" not in key for key in keys)
        concurrency_key = next(key for key in keys if ":concurrent:" in key)
        assert 0 < await redis.ttl(concurrency_key) <= 150
        await first.release()
        third = await limiter.acquire(direct_ip="127.0.0.1", forwarded_for="198.51.100.23")
        await second.release()
        await third.release()

        await redis.flushdb()
        rate_limiter = RedisGuestChatLimiter(
            redis,
            "integration-test-hmac-key-32-bytes-minimum",
            (),
            requests_per_hour=2,
            max_concurrent=1,
        )
        for _ in range(2):
            lease = await rate_limiter.acquire(direct_ip="192.0.2.9", forwarded_for=None)
            await lease.release()
        with pytest.raises(GuestLimitError) as rate:
            await rate_limiter.acquire(direct_ip="192.0.2.9", forwarded_for=None)
        assert rate.value.code == "RATE_LIMITED"
        rate_key = next(str(key) for key in await redis.keys("guest:chat:rate:*") )
        assert 0 < await redis.ttl(rate_key) <= 7200
        assert "192.0.2.9" not in rate_key
    finally:
        await redis.flushdb()
        await redis.aclose()
