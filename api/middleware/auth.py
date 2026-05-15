"""
API key authentication + per-key rate limiting.

Keys are read from the API_KEYS environment variable (comma-separated) at startup.
Each key gets its own daily investigation quota and hourly upload quota enforced via
Redis sliding windows.

Rate limit config (env vars):
  RATE_LIMIT_INVESTIGATIONS_PER_DAY  default 50
  RATE_LIMIT_UPLOADS_PER_HOUR        default 100

For MVP: keys are static (set via env). Add a key management endpoint later.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Callable

import structlog
from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader

from config.settings import settings

log = structlog.get_logger(__name__)

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


# ── Key registry ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _valid_keys() -> frozenset[str]:
    """Load API keys from settings once at startup."""
    raw = getattr(settings, "api_keys", "")
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    if not keys:
        log.warning(
            "api_key_auth_disabled",
            detail="API_KEYS env var not set — all requests are unauthenticated. "
                   "Set API_KEYS=key1,key2 before production deployment.",
        )
    return frozenset(keys)


def _keys_enabled() -> bool:
    return bool(_valid_keys())


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _redis():
    """Lazy Redis import to avoid import-time connection."""
    import redis as _redis
    return _redis.Redis.from_url(settings.redis_url, decode_responses=True)


def _check_rate_limit(key: str, action: str, limit: int, window_seconds: int) -> None:
    """
    Sliding-window rate limiter using Redis INCR + EXPIRE.

    Raises HTTP 429 if the key has exceeded `limit` calls of `action` in `window_seconds`.
    """
    try:
        r = _redis()
        bucket = int(time.time() / window_seconds)
        redis_key = f"ratelimit:{action}:{key}:{bucket}"
        count = r.incr(redis_key)
        if count == 1:
            r.expire(redis_key, window_seconds * 2)  # 2x window for safety
        if count > limit:
            log.warning(
                "rate_limit_exceeded",
                api_key=key[:8] + "...",
                action=action,
                count=count,
                limit=limit,
            )
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded: {limit} {action} per "
                       f"{window_seconds // 3600}h window. Retry after window resets.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Redis down → fail open (don't block requests if rate limit store is unavailable)
        log.error("rate_limit_redis_error", error=str(exc))


# ── FastAPI dependency ────────────────────────────────────────────────────────

async def require_api_key(
    request: Request,
    api_key: str | None = Security(_header_scheme),
) -> str:
    """
    FastAPI dependency — validates X-API-Key header.

    Returns the validated key string (use as Depends(require_api_key)).
    If API_KEYS is not configured, auth is disabled and a placeholder key is returned.
    """
    if not _keys_enabled():
        return "unauthenticated"

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    if api_key not in _valid_keys():
        log.warning(
            "invalid_api_key",
            ip=request.client.host if request.client else "unknown",
            key_prefix=api_key[:8] if len(api_key) >= 8 else "short",
        )
        raise HTTPException(status_code=403, detail="Invalid API key.")

    return api_key


def check_investigation_quota(api_key: str) -> None:
    """
    Enforce per-key daily investigation quota.
    Call from the start_investigation endpoint.
    """
    limit = getattr(settings, "rate_limit_investigations_per_day", 50)
    _check_rate_limit(api_key, "investigations", limit, window_seconds=86400)


def check_upload_quota(api_key: str) -> None:
    """
    Enforce per-key hourly upload quota.
    Call from the upload endpoint.
    """
    limit = getattr(settings, "rate_limit_uploads_per_hour", 100)
    _check_rate_limit(api_key, "uploads", limit, window_seconds=3600)


# ── Starlette middleware (optional — for non-route endpoints) ─────────────────

class APIKeyMiddleware:
    """
    ASGI middleware that blocks requests without a valid X-API-Key.

    Excluded paths: /health, /docs, /openapi.json (ALB health checks + Swagger UI)
    """

    EXCLUDED_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.EXCLUDED_PATHS or not _keys_enabled():
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        api_key = headers.get(b"x-api-key", b"").decode()

        if not api_key or api_key not in _valid_keys():
            response = _json_response(403, {"detail": "Invalid or missing X-API-Key"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _json_response(status: int, body: dict):
    """Minimal ASGI JSON response (avoids FastAPI import in middleware)."""
    import json
    from starlette.responses import JSONResponse
    return JSONResponse(status_code=status, content=body)
