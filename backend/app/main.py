import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from . import auth
from .config import settings
from .db import init_db
from .routers import chat, plans, ratings

# Логи приложения (plan via DeepSeek, валидатор, ошибки провайдеров) видны в контейнере.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("easy_week").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    auth.log_startup_state()
    yield


app = FastAPI(title="Easy Week API", lifespan=lifespan)

# Вход по общему паролю: закрывает /api/* (кроме /api/auth/*, /api/health).
# Добавляем ДО CORS — тогда CORS внешний и 401 тоже получает CORS-заголовки.
app.add_middleware(auth.AuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(plans.router)
app.include_router(ratings.router)

# Prometheus: HTTP-метрики (кол-во/задержка/статусы) + свои счётчики токенов (observe.py).
# /metrics слушается только локально (Prometheus на том же хосте скрапит 127.0.0.1:8010).
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


@app.get("/api/health", tags=["health"])
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "cfConfigured": settings.cf_configured,
        "model": settings.cf_model,
    }
