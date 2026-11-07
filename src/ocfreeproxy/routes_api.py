from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import require_access
from .models_client import (
    ModelsClient,
    NoProxyAvailableError,
    UpstreamHTTPError,
)
from .proxy_pool import ProxyPool
from .store import Store
from .subscription import (
    fetch_subscription_url,
    nodes_to_proxy_entries,
    parse_subscription_content,
    summarize_nodes,
)

router = APIRouter()


def _store(request: Request) -> Store:
    return request.app.state.store


def _pool(request: Request) -> ProxyPool:
    return request.app.state.pool


def _models(request: Request) -> ModelsClient:
    return request.app.state.models_client


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "ocfreeproxy", "ts": time.time()}


@router.get("/v1/models", dependencies=[Depends(require_access)])
async def list_models(request: Request, refresh: bool = False) -> dict[str, Any]:
    client = _models(request)
    try:
        models = await client.fetch_models(force=refresh)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {exc}") from exc
    return {"object": "list", "data": models}


@router.post("/v1/chat/completions", dependencies=[Depends(require_access)])
async def chat_completions(request: Request) -> Any:
    client = _models(request)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    stream = bool(body.get("stream"))
    try:
        result, selected = await client.chat_completions(body, stream=stream)
    except NoProxyAvailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UpstreamHTTPError as exc:
        return JSONResponse(
            status_code=exc.status_code if 400 <= exc.status_code < 600 else 502,
            content={
                "error": {
                    "message": exc.body[:2000] or str(exc),
                    "type": "upstream_error",
                    "proxy_id": exc.selected.entry.id if exc.selected else None,
                    "proxy_name": exc.selected.entry.name if exc.selected else None,
                }
            },
        )

    headers = {
        "X-OCFreeProxy-Egress": selected.entry.id,
        "X-OCFreeProxy-Egress-Name": selected.entry.name,
    }
    if stream:
        assert hasattr(result, "aiter_bytes")
        return StreamingResponse(
            result.aiter_bytes(),
            status_code=result.status_code,
            media_type="text/event-stream",
            headers=headers,
        )

    media = result.headers.get("content-type", "application/json")
    if "json" in (media or ""):
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError:
            payload = {"raw": result.text}
    else:
        payload = {"raw": result.text}
    return JSONResponse(status_code=result.status_code, content=payload, headers=headers)


# ----- Admin / UI JSON APIs -----

admin = APIRouter(prefix="/api", tags=["admin"])


@admin.get("/status")
async def api_status(request: Request) -> dict[str, Any]:
    pool = _pool(request)
    store = _store(request)
    settings = request.app.state.settings
    return {
        "use_direct": pool.use_direct,
        "upstream_base_url": settings.upstream_base_url,
        "access_token_set": bool(settings.access_token),
        "pool": pool.status(),
        "subscriptions": [s.to_dict() for s in store.list_subscriptions()],
    }


class DirectToggle(BaseModel):
    enabled: bool


@admin.post("/settings/use_direct")
async def set_use_direct(body: DirectToggle, request: Request) -> dict[str, Any]:
    pool = _pool(request)
    pool.set_use_direct(body.enabled)
    return {"ok": True, "use_direct": pool.use_direct}


class UpstreamBody(BaseModel):
    upstream_base_url: str = Field(min_length=8)


@admin.post("/settings/upstream")
async def set_upstream(body: UpstreamBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    url = body.upstream_base_url.strip().rstrip("/")
    store.set_setting("upstream_base_url", url)
    request.app.state.settings.upstream_base_url = url
    return {"ok": True, "upstream_base_url": url}


@admin.get("/models")
async def api_models(request: Request, refresh: bool = False) -> dict[str, Any]:
    client = _models(request)
    models = await client.fetch_models(force=refresh)
    return {"object": "list", "data": models, "count": len(models)}


@admin.get("/proxies")
async def api_proxies(request: Request) -> dict[str, Any]:
    return _pool(request).status()


class ManualProxyBody(BaseModel):
    name: str = ""
    proxy_url: str


@admin.post("/proxies")
async def add_manual_proxy(body: ManualProxyBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    url = body.proxy_url.strip()
    if not url:
        raise HTTPException(400, "proxy_url required")
    lower = url.lower()
    if lower.startswith(("socks5://", "socks5h://", "socks4://")):
        protocol = "socks5"
    elif lower.startswith(("http://", "https://")):
        protocol = "http"
    else:
        raise HTTPException(400, "仅支持 http(s):// 或 socks5:// URL")
    name = body.name.strip() or url
    entry = store.add_proxy(name=name, proxy_url=url, protocol=protocol, source="manual")
    return {"ok": True, "proxy": entry.to_dict()}


@admin.post("/proxies/{proxy_id}/enable")
async def enable_proxy(proxy_id: str, request: Request, enabled: bool = Query(True)) -> dict[str, Any]:
    store = _store(request)
    if proxy_id == "direct":
        raise HTTPException(400, "请使用「使用本地连接」开关控制 Direct")
    p = store.get_proxy(proxy_id)
    if not p:
        raise HTTPException(404, "proxy not found")
    store.set_proxy_enabled(proxy_id, enabled)
    return {"ok": True}


@admin.post("/proxies/{proxy_id}/clear_cooldown")
async def clear_proxy_cooldown(proxy_id: str, request: Request) -> dict[str, Any]:
    _pool(request).clear_cooldown(proxy_id)
    return {"ok": True}


@admin.delete("/proxies/{proxy_id}")
async def delete_proxy(proxy_id: str, request: Request) -> dict[str, Any]:
    if proxy_id == "direct":
        raise HTTPException(400, "不能删除 Direct")
    _store(request).delete_proxy(proxy_id)
    return {"ok": True}


class ClashSubBody(BaseModel):
    name: str = ""
    url: str


class PasteSubBody(BaseModel):
    name: str = ""
    content: str


@admin.post("/subscriptions/clash")
async def add_clash_subscription(body: ClashSubBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "需要 http(s) 订阅链接")
    sub = store.add_subscription(
        name=body.name.strip() or "Clash 订阅",
        kind="clash_url",
        content=url,
    )
    result = await _refresh_one(store, sub.id)
    sub = store.get_subscription(sub.id) or sub
    return {"ok": True, "subscription": sub.to_dict(), "refresh": result}


@admin.post("/subscriptions/paste")
async def add_paste_subscription(body: PasteSubBody, request: Request) -> dict[str, Any]:
    store = _store(request)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "内容不能为空")
    # If pasted a bare URL, treat as clash_url
    kind = "clash_url" if content.startswith(("http://", "https://")) and "\n" not in content else "paste"
    sub = store.add_subscription(
        name=body.name.strip() or ("订阅链接" if kind == "clash_url" else "粘贴订阅"),
        kind=kind,
        content=content,
    )
    result = await _refresh_one(store, sub.id)
    sub = store.get_subscription(sub.id) or sub
    return {"ok": True, "subscription": sub.to_dict(), "refresh": result}


@admin.post("/subscriptions/{sub_id}/refresh")
async def refresh_subscription(sub_id: str, request: Request) -> dict[str, Any]:
    return await _refresh_one(_store(request), sub_id)


@admin.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: str, request: Request) -> dict[str, Any]:
    _store(request).delete_subscription(sub_id)
    return {"ok": True}


async def _refresh_one(store: Store, sub_id: str) -> dict[str, Any]:
    sub = store.get_subscription(sub_id)
    if not sub:
        raise HTTPException(404, "subscription not found")
    try:
        if sub.kind == "clash_url" or (
            sub.kind == "paste"
            and sub.content.startswith(("http://", "https://"))
            and "\n" not in sub.content
        ):
            raw = await fetch_subscription_url(sub.content)
        else:
            raw = sub.content
        nodes = parse_subscription_content(raw)
        summary = summarize_nodes(nodes)
        entries = nodes_to_proxy_entries(
            nodes, source=sub.kind, subscription_id=sub.id, include_unsupported=True
        )
        store.replace_subscription_proxies(sub.id, entries)
        sub.last_refresh_at = time.time()
        sub.last_error = ""
        store.update_subscription(sub)
        return {"ok": True, "summary": summary, "imported": len(entries)}
    except Exception as exc:
        sub.last_error = str(exc)
        store.update_subscription(sub)
        return {"ok": False, "error": str(exc)}
