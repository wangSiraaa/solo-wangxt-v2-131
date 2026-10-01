from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analyses, calibrations, manifests
from app.db.session import init_db

app = FastAPI(
    title="Power Quality Offline Review Platform",
    version="1.0.0",
    description="Immutable raw blocks, versioned channel calibration, and one-shot asynchronous reports.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(manifests.router)
app.include_router(calibrations.router)
app.include_router(analyses.router)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "power-quality-offline-review"}
