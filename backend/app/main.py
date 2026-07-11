from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import init_db
from .routers import chat, plans


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Easy Week API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(plans.router)


@app.get("/api/health", tags=["health"])
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "cfConfigured": settings.cf_configured,
        "model": settings.cf_model,
    }
