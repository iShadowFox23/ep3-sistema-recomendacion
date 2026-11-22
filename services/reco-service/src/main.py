"""reco-service: API de recomendaciones para el widget de Shopify (home, ficha de producto, carrito).

GET /v1/recommendations?user_id=257597&context=home&k=10[&item_id=355908&item_id=...][&category_id=1532]
Header: X-Api-Key: <clave de la tienda>

Componentes (C4 nivel 3):
    API Controller        -> endpoint y validación
    Autenticación         -> API key por tienda (`require_api_key`)
    Recuperador/caché     -> `Cache` (Redis/ElastiCache o memoria, TTL 15 min)
    Cliente de ranking    -> HTTP a rank-service con timeout (por defecto 150 ms)
    Fallback Handler      -> populares con stock si rank-service/SageMaker no responden
    Telemetría            -> X-Request-ID propagado, X-Response-Time-ms y logs JSON

Variables de entorno:
    RANK_SERVICE_URL   (por defecto http://localhost:8081)
    RANK_TIMEOUT_MS    (por defecto 150)
    API_KEYS           claves válidas separadas por coma (en ECS se inyectan desde Secrets Manager);
                       vacío = autenticación desactivada (solo desarrollo local)
    REDIS_URL          (vacío = caché en memoria)
    CACHE_TTL_S        (por defecto 900)
    POPULAR_PATH       ruta local o s3:// a popular.json (fallback)
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .cache import build_cache
from .fallback import Fallback

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","service":"reco-service","msg":%(message)s}')
log = logging.getLogger("reco-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    if not hasattr(state, "cache"):
        state.cache = build_cache(os.getenv("REDIS_URL") or None, int(os.getenv("CACHE_TTL_S", "900")))
    if not hasattr(state, "fallback"):
        state.fallback = Fallback.load(os.getenv("POPULAR_PATH") or None)
    if not hasattr(state, "api_keys"):
        state.api_keys = {k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()}
    timeout = float(os.getenv("RANK_TIMEOUT_MS", "150")) / 1000
    owns_http = not hasattr(state, "http")
    if owns_http:
        state.http = httpx.AsyncClient(base_url=os.getenv("RANK_SERVICE_URL", "http://localhost:8081"), timeout=timeout)
    if not state.api_keys:
        log.warning('"API_KEYS vacío: autenticación desactivada (solo para desarrollo local)"')
    log.info('"listo: cache=%s, rank_timeout_ms=%d"', type(state.cache).__name__, timeout * 1000)
    yield
    if owns_http:
        await state.http.aclose()


app = FastAPI(title="reco-service", version="1.0.0", lifespan=lifespan)


def error_body(code: str, message: str, request: Request) -> dict:
    return {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first.get("loc", [])[1:])
    return JSONResponse(status_code=422, content=error_body("invalid_request", f"{field}: {first['msg']}", request))


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException):
    code, message = exc.detail if isinstance(exc.detail, tuple) else ("error", str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content=error_body(code, message, request))


@app.middleware("http")
async def telemetry(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["x-request-id"] = request.state.request_id
    response.headers["x-response-time-ms"] = f"{elapsed:.1f}"
    if request.url.path != "/health":
        log.info(
            '{"request_id":"%s","path":"%s","status":%d,"ms":%.1f}',
            request.state.request_id, request.url.path, response.status_code, elapsed,
        )
    return response


def require_api_key(request: Request) -> None:
    keys = request.app.state.api_keys
    if not keys:
        return
    provided = request.headers.get("x-api-key")
    if not provided:
        raise HTTPException(status_code=401, detail=("missing_api_key", "Falta el header X-Api-Key"))
    if provided not in keys:
        raise HTTPException(status_code=403, detail=("invalid_api_key", "API key inválida"))


@app.get("/health")
def health():
    return {"status": "ok", "service": "reco-service"}


@app.get("/v1/recommendations", dependencies=[Depends(require_api_key)])
async def recommendations(
    request: Request,
    user_id: int | None = None,
    context: Literal["home", "pdp", "cart"] = "home",
    k: int = Query(10, ge=1, le=20),
    item_id: list[int] = Query([], max_length=50, description="productos de la ficha o del carrito"),
    category_id: int | None = None,
):
    state = request.app.state
    payload = {"user_id": user_id, "context": context, "k": k, "recent_items": item_id, "category_id": category_id}
    payload = {key: value for key, value in payload.items() if value is not None}
    cache_key = "reco:" + hashlib.sha1(repr(sorted(payload.items())).encode()).hexdigest()

    cached = state.cache.get(cache_key)
    if cached is not None:
        return {**cached, "request_id": request.state.request_id, "cache": "hit"}

    degraded, reason = False, None
    try:
        resp = await state.http.post("/v1/rank", json=payload, headers={"x-request-id": request.state.request_id})
        resp.raise_for_status()
        ranked = resp.json()
        items, strategy, cold_start, rules = ranked["items"], ranked["strategy"], ranked["cold_start"], ranked.get("rules")
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        degraded = True
        reason = "rank_timeout" if isinstance(exc, httpx.TimeoutException) else "rank_error"
        log.warning('{"request_id":"%s","fallback":"%s","detail":"%s"}', request.state.request_id, reason, type(exc).__name__)
        items, strategy = state.fallback.recommend(k, category_id, set(item_id))
        cold_start, rules = True, None

    body = {
        "user_id": user_id,
        "context": context,
        "items": items,
        "strategy": strategy,
        "cold_start": cold_start,
        "degraded": degraded,
        "degraded_reason": reason,
        "rules": rules,
    }
    if not degraded:  # no se cachean respuestas de respaldo: al recuperarse el modelo, se vuelve a personalizar
        state.cache.set(cache_key, body)
    return {**body, "request_id": request.state.request_id, "cache": "miss"}
