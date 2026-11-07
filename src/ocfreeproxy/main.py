from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings, get_settings
from .models_client import ModelsClient
from .proxy_pool import ProxyPool
from .routes_api import admin, router as api_router
from .routes_web import router as web_router
from .store import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        # Persist / restore use_direct
        if store.get_setting("use_direct") is None:
            store.set_bool("use_direct", settings.use_direct)
        stored_upstream = store.get_setting("upstream_base_url")
        if stored_upstream:
            settings.upstream_base_url = stored_upstream
        pool = ProxyPool(store)
        models_client = ModelsClient(settings, pool)
        app.state.settings = settings
        app.state.store = store
        app.state.pool = pool
        app.state.models_client = models_client
        yield

    app = FastAPI(
        title="OCFreeProxy",
        description="OpenCode Free model reverse proxy",
        version="0.1.0",
        lifespan=lifespan,
    )

    pkg_dir = Path(__file__).resolve().parent
    static_dir = pkg_dir / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(web_router)
    app.include_router(api_router)
    app.include_router(admin)
    return app


app = create_app()


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "ocfreeproxy.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()
