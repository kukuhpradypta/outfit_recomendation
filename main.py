"""FastAPI application entry point for the AI Outfit Recommendation backend."""
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.routers import categories, items, recommendations
from app.services import composite_service, storage_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic (run `alembic upgrade head`). Here we only
    # ensure the image directories exist.
    storage_service.ensure_dirs()
    # Load the background-removal model in the background so the first
    # recommendation does not pay the ~5s model load.
    threading.Thread(target=composite_service.warm_up, daemon=True).start()
    logger.info("Startup complete. Text model=%s, Image model=%s",
                settings.gemini_text_model, settings.gemini_image_model)
    yield


app = FastAPI(
    title="AI Outfit Recommendation API",
    description=(
        "Backend rekomendasi outfit berbasis katalog produk, budget, foto pengguna, "
        "dan Gemini. Dokumentasi interaktif (Scalar) tersedia di /docs."
    ),
    version="1.0.0",
    lifespan=lifespan,
    # Nonaktifkan Swagger UI & ReDoc bawaan; /docs dipakai Scalar.
    docs_url=None,
    redoc_url=None,
)

# Serve uploaded and generated images.
Path(settings.public_dir).mkdir(parents=True, exist_ok=True)
app.mount("/public", StaticFiles(directory=settings.public_dir), name="public")

# Routers
app.include_router(categories.router)
app.include_router(items.router)
app.include_router(recommendations.router)


@app.get("/", tags=["meta"])
def root():
    return {
        "name": "AI Outfit Recommendation API",
        "docs": "/docs",
        "openapi": "/openapi.json",
    }


# Scalar API Reference GUI
try:
    from scalar_fastapi import get_scalar_api_reference

    @app.get("/docs", include_in_schema=False)
    def scalar_html():
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=app.title,
        )
except ImportError:  # fallback if scalar-fastapi is unavailable
    @app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
    def scalar_html():  # type: ignore[misc]
        return HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<title>API Reference</title></head><body>'
            f'<script id="api-reference" data-url="{app.openapi_url}"></script>'
            '<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>'
            "</body></html>"
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.app_host, port=settings.app_port, reload=True)
