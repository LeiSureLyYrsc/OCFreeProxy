from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    settings = request.app.state.settings
    pool = request.app.state.pool
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "use_direct": pool.use_direct,
            "upstream_base_url": settings.upstream_base_url,
            "access_token_set": bool(settings.access_token),
        },
    )
