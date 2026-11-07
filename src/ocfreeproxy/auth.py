from __future__ import annotations

from fastapi import Header, HTTPException, Request

from .config import Settings


async def require_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    settings: Settings = request.app.state.settings
    token = (settings.access_token or "").strip()
    if not token:
        return
    provided = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    elif x_api_key:
        provided = x_api_key.strip()
    if provided != token:
        raise HTTPException(status_code=401, detail="Invalid or missing ACCESS_TOKEN")
