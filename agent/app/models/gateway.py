"""Credential-isolated, no-redirect HTTP boundary shared by model adapters."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.telemetry import propagation_headers


def gateway_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        path = unquote(parsed.path)
        if (value != value.strip() or any(c.isspace() for c in value)
                or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "\\" in path or any(segment in {".", ".."} for segment in path.split("/"))):
            raise ValueError()
        _ = parsed.port
    except ValueError:
        raise ValueError("invalid model gateway URL (credentials/query/fragment forbidden)") from None
    return value.rstrip("/")


class GatewayRequestError(RuntimeError):
    pass


class GatewayHTTP:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: float,
                 *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = gateway_url(base_url)
        if not api_key.strip() or timeout_seconds <= 0:
            raise ValueError("model credentials and positive timeout required")
        self._key = api_key
        self.timeout = timeout_seconds
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)
        self._owns_client = client is None

    async def post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            # asyncio.timeout is a total wall-clock budget across connect/write/
            # all response chunks, not only httpx's per-operation idle timeout.
            async with asyncio.timeout(self.timeout):
                response = await self.client.post(
                    self.base_url + "/" + route.lstrip("/"), json=payload,
                    headers={**propagation_headers(), "Authorization": f"Bearer {self._key}"},
                    follow_redirects=False,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except (httpx.HTTPError, TimeoutError, ValueError):
            # Never chain HTTP exceptions containing request headers/body/URL.
            raise GatewayRequestError("model gateway request failed") from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
