from __future__ import annotations

import hashlib
import hmac
import ipaddress
import time
import uuid
import re
from dataclasses import dataclass
from typing import Protocol

from redis.asyncio import Redis


class GuestLimitError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class GuestChatLease(Protocol):
    async def release(self) -> None: ...


class GuestChatLimiter(Protocol):
    async def acquire(
        self,
        *,
        direct_ip: str,
        forwarded_for: str | None,
    ) -> GuestChatLease: ...


@dataclass
class _RedisGuestLease:
    redis: Redis
    concurrency_key: str
    slot_id: str
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            await self.redis.zrem(self.concurrency_key, self.slot_id)
        except Exception:
            # The bounded lease expires without intervention; release is best effort.
            return


class RedisGuestChatLimiter:
    _ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local lease_until = tonumber(ARGV[2])
local max_requests = tonumber(ARGV[3])
local max_concurrent = tonumber(ARGV[4])
local slot_id = ARGV[5]
local rate_ttl = tonumber(ARGV[6])
local lease_ttl = tonumber(ARGV[7])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local requests = tonumber(redis.call('GET', KEYS[1]) or '0')
if requests >= max_requests then
  return 1
end
if redis.call('ZCARD', KEYS[2]) >= max_concurrent then
  return 2
end
requests = redis.call('INCR', KEYS[1])
if requests == 1 then
  redis.call('EXPIRE', KEYS[1], rate_ttl)
end
redis.call('ZADD', KEYS[2], lease_until, slot_id)
redis.call('EXPIRE', KEYS[2], lease_ttl)
return 0
"""

    def __init__(
        self,
        redis: Redis,
        hmac_key: str,
        trusted_proxy_cidrs: tuple[str, ...],
        *,
        requests_per_hour: int = 20,
        max_concurrent: int = 2,
        lease_seconds: int = 150,
    ) -> None:
        if len(hmac_key.encode("utf-8")) < 32:
            raise ValueError("guest rate-limit HMAC key must be at least 32 bytes")
        if requests_per_hour < 1 or max_concurrent < 1:
            raise ValueError("guest rate limits must be positive")
        self.redis = redis
        self.hmac_key = hmac_key.encode("utf-8")
        self.trusted_proxies = tuple(ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs)
        self.requests_per_hour = requests_per_hour
        self.max_concurrent = max_concurrent
        self.lease_seconds = lease_seconds

    async def acquire(
        self,
        *,
        direct_ip: str,
        forwarded_for: str | None,
    ) -> GuestChatLease:
        source = self._source_ip(direct_ip, forwarded_for)
        fingerprint = hmac.new(
            self.hmac_key,
            source.compressed.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        now = int(time.time())
        bucket = now // 3600
        concurrency_key = f"guest:chat:concurrent:{fingerprint}"
        rate_key = f"guest:chat:rate:{fingerprint}:{bucket}"
        slot_id = uuid.uuid4().hex
        try:
            result = int(
                await self.redis.eval(
                    self._ACQUIRE_SCRIPT,
                    2,
                    rate_key,
                    concurrency_key,
                    now,
                    now + self.lease_seconds,
                    self.requests_per_hour,
                    self.max_concurrent,
                    slot_id,
                    7200,
                    self.lease_seconds,
                )
            )
        except GuestLimitError:
            raise
        except Exception as exc:
            raise GuestLimitError(
                "DEPENDENCY_UNAVAILABLE",
                "guest rate limiter is unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        if result in {1, 2}:
            raise GuestLimitError(
                "RATE_LIMITED",
                "guest chat rate limit exceeded",
                status_code=429,
                retryable=True,
            )
        if result != 0:
            raise GuestLimitError(
                "DEPENDENCY_UNAVAILABLE",
                "guest rate limiter returned an invalid result",
                status_code=503,
                retryable=True,
            )
        return _RedisGuestLease(self.redis, concurrency_key, slot_id)

    def _source_ip(self, direct_ip: str, forwarded_for: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
        try:
            direct = ipaddress.ip_address(direct_ip.strip())
        except ValueError as exc:
            raise GuestLimitError(
                "DEPENDENCY_UNAVAILABLE",
                "guest source address is unavailable",
                status_code=503,
                retryable=True,
            ) from exc
        if not any(direct in network for network in self.trusted_proxies):
            return direct
        if forwarded_for is None or not forwarded_for.strip():
            return direct
        # The trusted reverse proxy must overwrite this header with exactly one address.
        if "," in forwarded_for:
            raise GuestLimitError(
                "DEPENDENCY_UNAVAILABLE",
                "trusted proxy supplied an invalid source address",
                status_code=503,
                retryable=True,
            )
        try:
            return ipaddress.ip_address(forwarded_for.strip())
        except ValueError as exc:
            raise GuestLimitError(
                "DEPENDENCY_UNAVAILABLE",
                "trusted proxy supplied an invalid source address",
                status_code=503,
                retryable=True,
            ) from exc


def parse_trusted_proxy_cidrs(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    for item in values:
        ipaddress.ip_network(item, strict=False)
    return values


_AUTHENTICATED_CAPABILITY_PATTERNS = (
    r"下单",
    r"购买",
    r"买这",
    r"我的订单",
    r"订单查询",
    r"查.*订单",
    r"补货监听",
    r"到货提醒",
    r"长期记忆",
    r"删除.*记忆",
    r"轨迹",
    r"回放",
    r"\bbuy\b",
    r"\bplace (?:an )?order\b",
    r"\bmy orders?\b",
    r"\bmemory\b",
    r"\btrace\b",
    r"\breplay\b",
    r"\brestock (?:watch|alert)\b",
)


def guest_login_required(content: str) -> bool:
    normalized = content.casefold()
    return any(re.search(pattern, normalized) for pattern in _AUTHENTICATED_CAPABILITY_PATTERNS)
