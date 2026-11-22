"""rank-service: pide candidatos al endpoint de SageMaker y aplica las reglas de negocio.

POST /v1/rank
    {"user_id": 257597, "context": "home|pdp|cart", "k": 10,
     "recent_items": [355908], "category_id": 1532}
-> {"items": [...], "strategy": "als", "cold_start": false,
    "rules": {"removed_unavailable": 7, ...}, "candidates": 30, "model_ms": 24.1}

Variables de entorno:
    MODEL_BACKEND        sagemaker | stub            (por defecto: stub)
    SAGEMAKER_ENDPOINT   nombre del endpoint          (por defecto: reco-als)
    MODEL_TIMEOUT_S      timeout al endpoint          (por defecto: 1.0)
    CATALOG_PATH         ruta local o s3:// a catalog.csv (vacío = sin reglas de stock/categoría)
    CANDIDATE_MULTIPLIER candidatos pedidos = k x multiplicador (por defecto: 4)
    MAX_PER_CATEGORY     regla de diversidad          (por defecto: 3)
    FILTER_UNAVAILABLE   true | false                 (por defecto: true)
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .catalog import Catalog
from .model_client import ModelClient, ModelUnavailable, SageMakerModelClient, StubModelClient
from .rules import RuleConfig, apply_rules

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","service":"rank-service","msg":%(message)s}')
log = logging.getLogger("rank-service")

MAX_CANDIDATES = 50  # límite del endpoint


def build_model_client() -> ModelClient:
    if os.getenv("MODEL_BACKEND", "stub") == "sagemaker":
        return SageMakerModelClient(
            endpoint_name=os.getenv("SAGEMAKER_ENDPOINT", "reco-als"),
            region=os.getenv("AWS_REGION"),
            timeout_s=float(os.getenv("MODEL_TIMEOUT_S", "1.0")),
        )
    return StubModelClient()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not hasattr(app.state, "model"):
        app.state.model = build_model_client()
    if not hasattr(app.state, "catalog"):
        app.state.catalog = Catalog.load(os.getenv("CATALOG_PATH") or None)
    if not hasattr(app.state, "rules"):
        app.state.rules = RuleConfig(
            filter_unavailable=os.getenv("FILTER_UNAVAILABLE", "true").lower() == "true",
            max_per_category=int(os.getenv("MAX_PER_CATEGORY", "3")),
        )
    app.state.multiplier = int(os.getenv("CANDIDATE_MULTIPLIER", "4"))
    log.info('"listo: modelo=%s, catalogo=%d productos"', type(app.state.model).__name__, len(app.state.catalog))
    yield


app = FastAPI(title="rank-service", version="1.0.0", lifespan=lifespan)


class RankRequest(BaseModel):
    user_id: int | None = None
    context: Literal["home", "pdp", "cart"] = "home"
    k: int = Field(10, ge=1, le=20)
    recent_items: list[int] = Field(default_factory=list, max_length=50)
    category_id: int | None = None


def error_body(code: str, message: str, request: Request) -> dict:
    return {"error": {"code": code, "message": message, "request_id": request.headers.get("x-request-id")}}


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content=error_body("invalid_request", str(exc.errors()[0]["msg"]), request))


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException):
    code = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content=error_body(code, code, request))


@app.middleware("http")
async def telemetry(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["x-request-id"] = request_id
    response.headers["x-response-time-ms"] = f"{elapsed:.1f}"
    if request.url.path != "/health":
        log.info('{"request_id":"%s","path":"%s","status":%d,"ms":%.1f}', request_id, request.url.path, response.status_code, elapsed)
    return response


@app.get("/health")
def health():
    return {"status": "ok", "service": "rank-service"}


@app.post("/v1/rank")
async def rank(body: RankRequest, request: Request):
    k_candidates = min(body.k * request.app.state.multiplier, MAX_CANDIDATES)
    model_request = body.model_dump(exclude_none=True) | {"k": k_candidates}
    start = time.perf_counter()
    try:
        prediction = await run_in_threadpool(request.app.state.model.predict, model_request)
    except ModelUnavailable as exc:
        log.warning('{"request_id":"%s","error":"model_unavailable","detail":"%s"}', request.headers.get("x-request-id"), str(exc)[:200])
        raise HTTPException(status_code=503, detail="model_unavailable") from exc
    model_ms = (time.perf_counter() - start) * 1000

    result = apply_rules(
        prediction.get("items", []), request.app.state.catalog, body.k, set(body.recent_items), request.app.state.rules
    )
    return {
        "items": result.items,
        "strategy": prediction.get("strategy"),
        "cold_start": prediction.get("cold_start", False),
        "rules": result.stats.as_dict(),
        "candidates": len(prediction.get("items", [])),
        "model_ms": round(model_ms, 1),
        "model": prediction.get("model"),
    }
