from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .database import init_db
from .routers import analysis, manifests, preview

settings = get_settings()
app = FastAPI(title="Power Quality Offline Review Platform", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "object_store": settings.object_store}


app.include_router(manifests.router)
app.include_router(analysis.router)
app.include_router(preview.router)
