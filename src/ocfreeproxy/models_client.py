from __future__ import annotations

import time
from typing import Any, AsyncIterator

import httpx

from .config import Settings
from .cooldown import cooldown_seconds_from_response
from .proxy_pool import ProxyPool, SelectedProxy


def _cost_price(obj: Any) -> float | None:
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict) and "price" in obj:
        try:
            return float(obj["price"])
        except (TypeError, ValueError):
            return None
    return None


def is_free_model(model: dict[str, Any]) -> bool:
    mid = str(model.get("id") or "")
    if mid.endswith("-free"):
        return True
    cost = model.get("cost") or {}
    text = cost.get("text") if isinstance(cost, dict) else None
    if not isinstance(text, dict):
        return False
    inp = _cost_price(text.get("input") or {})
    out = _cost_price(text.get("output") or {})
    if inp is None or out is None:
        return False
    return inp == 0.0 and out == 0.0


class NoProxyAvailableError(Exception):
    pass


class UpstreamHTTPError(Exception):
    def __init__(
        self, status_code: int, body: str, selected: SelectedProxy | None = None
    ) -> None:
        super().__init__(f"upstream {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body
        self.selected = selected


class BufferedResponse:
    def __init__(self, status_code: int, content: bytes, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.content = content
        self.headers = headers

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class StreamingResponseWrapper:
    def __init__(self, response: httpx.Response, client: httpx.AsyncClient) -> None:
        self.response = response
        self.client = client
        self.status_code = response.status_code
        self.headers = dict(response.headers)

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.response.aclose()
            await self.client.aclose()


class ModelsClient:
    def __init__(self, settings: Settings, pool: ProxyPool) -> None:
        self.settings = settings
        self.pool = pool
        self._models_cache: list[dict[str, Any]] | None = None
        self._models_cached_at: float = 0.0

    def _upstream_headers(self, stream: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "OCFreeProxy/0.1",
        }
        if self.settings.opencode_api_key:
            headers["Authorization"] = f"Bearer {self.settings.opencode_api_key}"
        return headers

    def _make_client(self, proxy: str | None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.settings.request_timeout),
            "follow_redirects": True,
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def fetch_models(self, force: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if (
            not force
            and self._models_cache is not None
            and now - self._models_cached_at < self.settings.models_cache_ttl
        ):
            return self._models_cache

        url = f"{self.settings.upstream_base_url.rstrip('/')}/models"
        async with self._make_client(None) as client:
            resp = await client.get(url, headers=self._upstream_headers())
            resp.raise_for_status()
            payload = resp.json()

        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            data = []
        free = [m for m in data if isinstance(m, dict) and is_free_model(m)]
        self._models_cache = free
        self._models_cached_at = now
        return free

    def _apply_cooldown(
        self, selected: SelectedProxy, status_code: int, retry_after: str | None, body: str
    ) -> bool:
        seconds = cooldown_seconds_from_response(status_code, retry_after, body)
        if seconds is None:
            return False
        reason = f"HTTP {status_code}"
        if "freeusagelimit" in body.lower() or "FreeUsageLimit" in body:
            reason = "FreeUsageLimit"
        self.pool.mark_cooldown(selected.entry.id, seconds, reason)
        return True

    async def chat_completions(
        self,
        body: dict[str, Any],
        *,
        stream: bool = False,
        max_retries: int = 8,
    ) -> tuple[BufferedResponse | StreamingResponseWrapper, SelectedProxy]:
        url = f"{self.settings.upstream_base_url.rstrip('/')}/chat/completions"
        last_error: UpstreamHTTPError | None = None
        tried: set[str] = set()

        for _ in range(max_retries):
            selected = self.pool.select()
            if selected is None:
                break
            if selected.entry.id in tried and len(tried) >= len(self.pool.available()):
                break
            tried.add(selected.entry.id)

            client = self._make_client(selected.httpx_proxy)
            headers = self._upstream_headers(stream=stream)
            try:
                if stream:
                    req = client.build_request("POST", url, json=body, headers=headers)
                    resp = await client.send(req, stream=True)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        err_body = (await resp.aread()).decode("utf-8", errors="replace")
                        retry_after = resp.headers.get("Retry-After") or resp.headers.get(
                            "retry-after"
                        )
                        cooled = self._apply_cooldown(
                            selected, resp.status_code, retry_after, err_body
                        )
                        await resp.aclose()
                        await client.aclose()
                        last_error = UpstreamHTTPError(resp.status_code, err_body, selected)
                        if cooled or resp.status_code >= 500:
                            continue
                        raise last_error
                    if resp.status_code >= 400:
                        err_body = (await resp.aread()).decode("utf-8", errors="replace")
                        await resp.aclose()
                        await client.aclose()
                        raise UpstreamHTTPError(resp.status_code, err_body, selected)
                    return StreamingResponseWrapper(resp, client), selected

                resp = await client.post(url, json=body, headers=headers)
                text = resp.text
                retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                if resp.status_code == 429 or (
                    resp.status_code >= 400
                    and cooldown_seconds_from_response(resp.status_code, retry_after, text)
                ):
                    self._apply_cooldown(selected, resp.status_code, retry_after, text)
                    await client.aclose()
                    last_error = UpstreamHTTPError(resp.status_code, text, selected)
                    continue
                if resp.status_code >= 500:
                    await client.aclose()
                    last_error = UpstreamHTTPError(resp.status_code, text, selected)
                    continue
                if resp.status_code >= 400:
                    await client.aclose()
                    raise UpstreamHTTPError(resp.status_code, text, selected)
                buffered = BufferedResponse(resp.status_code, resp.content, dict(resp.headers))
                await client.aclose()
                return buffered, selected
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                await client.aclose()
                last_error = UpstreamHTTPError(502, str(exc), selected)
                # brief soft cooldown so we rotate away from broken egress
                self.pool.mark_cooldown(selected.entry.id, 30, f"transport: {exc}")
                continue

        if last_error:
            raise last_error
        raise NoProxyAvailableError(
            "代理池中没有可用出口。请添加 HTTP/SOCKS5 代理，或开启「使用本地连接」。"
        )
