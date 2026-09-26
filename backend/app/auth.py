"""Простая авторизация по общему паролю (один пароль на все устройства).

- POST /api/auth/login {password} → сверка constant-time, ставим HttpOnly-куку `ew_session`
  с HMAC-подписанным токеном (живёт ~год).
- GET /api/auth/status → {authenticated, required}; POST /api/auth/logout → чистим куку.
- AuthMiddleware закрывает все /api/* кроме /api/auth/* и /api/health (401 JSON).
  /metrics не трогаем: Prometheus скрапит бэкенд напрямую по 127.0.0.1, а снаружи его
  ограничивает nginx.

Пароль — только из env `APP_PASSWORD`. Пустой → авторизация выключена (дев).
"""

import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings

log = logging.getLogger("easy_week.auth")

COOKIE_NAME = "ew_session"
COOKIE_MAX_AGE = 365 * 24 * 3600  # ~год: вошли один раз на устройстве — и забыли
TOKEN_VERSION = "v1"

# Пути /api, открытые без сессии (префиксы).
PUBLIC_PREFIXES = ("/api/auth/", "/api/health")

# Лимит неудачных попыток входа: не больше N за окно (сек) с одного IP.
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW = 60.0


# ---------- Токен сессии ----------


def _signing_key() -> bytes:
    """Ключ HMAC. Пароль подмешиваем всегда — смена APP_PASSWORD инвалидирует все сессии,
    даже если APP_SECRET задан отдельно."""
    base = (settings.app_secret or settings.app_password).encode()
    return hmac.new(base, b"ew-session|" + settings.app_password.encode(), hashlib.sha256).digest()


def _sign(payload: str) -> str:
    return hmac.new(_signing_key(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    """Токен вида `v1.<unix-ts>.<hmac>`. Внутри нет секретов — только время выдачи."""
    payload = f"{TOKEN_VERSION}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return False
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sign(payload), parts[2]):
        return False
    try:
        issued = int(parts[1])
    except ValueError:
        return False
    # Протухание по времени выдачи (кука и так живёт год, но токен не должен жить вечно).
    return time.time() - issued <= COOKIE_MAX_AGE


def is_authenticated(request: Request) -> bool:
    if not settings.auth_enabled:
        return True
    return verify_token(request.cookies.get(COOKIE_NAME))


# ---------- IP клиента и rate limit ----------


def _is_trusted_proxy(ip: str) -> bool:
    """Локальные прокси (nginx / tailscale funnel на том же Пае) — их адреса пропускаем."""
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    """Реальный IP клиента. X-Forwarded-For доверяем только если к нам пришёл локальный
    прокси (nginx); берём самый правый не-loopback адрес — его дописал наш прокси, а не клиент."""
    peer = request.client.host if request.client else "unknown"
    if not _is_trusted_proxy(peer):
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    for ip in reversed([p.strip() for p in xff.split(",") if p.strip()]):
        if not _is_trusted_proxy(ip):
            return ip
    return peer


class _RateLimiter:
    """In-memory счётчик неудачных попыток по IP (скользящее окно). Один процесс uvicorn — ок."""

    def __init__(self, attempts: int, window: float) -> None:
        self.attempts = attempts
        self.window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            q = self._prune(key, time.monotonic())
            if not q:
                self._hits.pop(key, None)  # не копим пустые ключи
            return len(q) >= self.attempts

    def hit(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


login_limiter = _RateLimiter(RATE_LIMIT_ATTEMPTS, RATE_LIMIT_WINDOW)


# ---------- Роутер ----------


class LoginBody(BaseModel):
    password: str


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _cookie_secure(request: Request) -> bool:
    mode = settings.auth_cookie_secure.strip().lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    # auto: https напрямую или за прокси (nginx/tailscale передают X-Forwarded-Proto)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip().lower() == "https"


@router.get("/status")
async def status(request: Request) -> dict[str, bool]:
    return {"authenticated": is_authenticated(request), "required": settings.auth_enabled}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, bool]:
    if not settings.auth_enabled:
        return {"authenticated": True, "required": False}

    ip = client_ip(request)
    if login_limiter.blocked(ip):
        log.warning("auth: слишком много попыток входа с %s", ip)
        raise HTTPException(status_code=429, detail="Слишком много попыток, подождите минуту")

    if not hmac.compare_digest(body.password.encode(), settings.app_password.encode()):
        login_limiter.hit(ip)
        log.info("auth: неверный пароль с %s", ip)
        raise HTTPException(status_code=401, detail="Неверный пароль")

    login_limiter.reset(ip)
    response.set_cookie(
        COOKIE_NAME,
        make_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    log.info("auth: вход с %s", ip)
    return {"authenticated": True, "required": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    response.delete_cookie(
        COOKIE_NAME, path="/", httponly=True, secure=_cookie_secure(request), samesite="lax"
    )
    return {"authenticated": False, "required": settings.auth_enabled}


# ---------- Middleware ----------


class AuthMiddleware:
    """Чистый ASGI-middleware (не BaseHTTPMiddleware — тот мешает SSE-стримингу).
    Закрывает /api/* без валидной сессии, кроме публичных путей и CORS-preflight."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if (
            path.startswith("/api/")
            and not path.startswith(PUBLIC_PREFIXES)
            and scope.get("method") != "OPTIONS"
            and not is_authenticated(Request(scope))
        ):
            resp = JSONResponse({"detail": "Требуется вход"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def log_startup_state() -> None:
    """Предупреждаем, если приложение открыто без пароля."""
    if settings.auth_enabled:
        log.info("auth: вход по паролю включён")
    else:
        log.warning("auth: APP_PASSWORD не задан — авторизация ВЫКЛЮЧЕНА, API открыт всем")
