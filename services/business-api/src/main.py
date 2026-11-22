"""business-api: API REST empresarial del sistema de recomendación.

Expuesta vía API Gateway (REST API con API keys y usage plans) → VPC Link → NLB → ALB → ECS.
La autenticación y el rate limiting ocurren en API Gateway; esta API recibe además el header
`x-api-key-id` (identificador de la key, mapeado por API Gateway) para registrar qué cliente
pidió cada reporte. Documentación interactiva en /docs (Swagger) y contrato en /openapi.json.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from enum import Enum
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .broker import BrokerUnavailable, Publisher
from .insights import daily_rows, kpi_summary, suggest_promotions
from .jobs import now
from .reports import REPORT_TYPES
from .settings import build_components, env, fault_injection_allowed
from .storage import LocalReportStore

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","service":"business-api","msg":%(message)s}')
log = logging.getLogger("business-api")

MAX_RANGE_DAYS = 180
DEFAULT_RANGE_DAYS = 14
TAGS = [
    {"name": "recomendaciones", "description": "Predicción en tiempo real con explicación (vía reco-service → rank-service → SageMaker)."},
    {"name": "insights", "description": "KPIs del embudo y acciones comerciales sugeridas (prescriptivo)."},
    {"name": "reportes", "description": "Reportes batch asíncronos: la API encola en RabbitMQ y report-worker los genera."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state
    if not hasattr(state, "components"):
        state.components = build_components()
    if not hasattr(state, "publisher"):
        state.publisher = Publisher(env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"))
    owns_http = not hasattr(state, "http")
    if owns_http:
        state.http = httpx.AsyncClient(base_url=env("RECO_SERVICE_URL", "http://localhost:8080"), timeout=3.0)
    state.reco_api_key = env("RECO_API_KEY", "")
    state.insights_cache = {}
    log.info('"listo: analytics=%s, jobs=%s"', type(state.components.analytics).__name__, type(state.components.jobs).__name__)
    yield
    if owns_http:
        await state.http.aclose()


app = FastAPI(
    title="business-api",
    version="1.0.0",
    description="API empresarial del sistema de recomendación de productos (Equipo 6, ITY1102). "
    "Autenticación: header `x-api-key` validado por API Gateway (usage plans con rate limiting).",
    openapi_tags=TAGS,
    lifespan=lifespan,
    # Detrás de API Gateway la API vive bajo la etapa (/prod): así Swagger arma bien sus URLs.
    root_path=env("ROOT_PATH", "") or "",
)


def custom_openapi():
    """Declara el esquema de API key en el contrato para que Swagger muestre el botón "Authorize".
    La validación real la hace API Gateway, no esta aplicación."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes, tags=TAGS, servers=app.servers
    )
    schema.setdefault("components", {})["securitySchemes"] = {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "x-api-key"}}
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


# ----------------------------------------------------------------------------- errores y telemetría


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None


class ErrorResponse(BaseModel):
    error: ErrorDetail


ERRORS = {
    422: {"model": ErrorResponse, "description": "Parámetros inválidos"},
    503: {"model": ErrorResponse, "description": "Dependencia no disponible"},
}


def error_response(status: int, code: str, message: str, request: Request) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    field = ".".join(str(p) for p in first.get("loc", [])[1:])
    message = f"{field}: {first['msg']}" if field else first["msg"]
    return error_response(422, "invalid_request", message, request)


@app.exception_handler(HTTPException)
async def http_handler(request: Request, exc: HTTPException):
    code, message = exc.detail if isinstance(exc.detail, tuple) else ("error", str(exc.detail))
    return error_response(exc.status_code, code, message, request)


@app.middleware("http")
async def telemetry(request: Request, call_next):
    request.state.request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.perf_counter()
    response = await call_next(request)
    elapsed = (time.perf_counter() - start) * 1000
    response.headers["x-request-id"] = request.state.request_id
    response.headers["x-response-time-ms"] = f"{elapsed:.1f}"
    if request.url.path not in ("/health", "/docs", "/openapi.json"):
        log.info(
            '{"request_id":"%s","method":"%s","path":"%s","status":%d,"ms":%.1f}',
            request.state.request_id, request.method, request.url.path, response.status_code, elapsed,
        )
    return response


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": "business-api"}


# ----------------------------------------------------------------------------- recomendaciones


class PredictRequest(BaseModel):
    user_id: int | None = Field(None, description="Visitante; si no existe en el modelo se trata como cold-start", examples=[257597])
    context: Literal["home", "pdp", "cart"] = "home"
    k: int = Field(10, ge=1, le=20)
    recent_items: list[int] = Field(default_factory=list, max_length=50, description="Producto en pantalla o carrito")
    category_id: int | None = Field(None, description="Categoría raíz para cold-start")


@app.post("/v1/recommendations/predict", tags=["recomendaciones"], responses=ERRORS)
async def predict(body: PredictRequest, request: Request):
    """Recomendaciones en tiempo real con la razón de cada producto, la estrategia usada
    (`als`, `item_similarity`, `popular_category`, `popular`) y las reglas de negocio aplicadas."""
    params = [("context", body.context), ("k", body.k)] + [("item_id", i) for i in body.recent_items]
    if body.user_id is not None:
        params.append(("user_id", body.user_id))
    if body.category_id is not None:
        params.append(("category_id", body.category_id))
    headers = {"x-request-id": request.state.request_id, "x-api-key": request.app.state.reco_api_key}
    try:
        resp = await request.app.state.http.get("/v1/recommendations", params=params, headers=headers)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning('{"request_id":"%s","error":"reco_unavailable","detail":"%s"}', request.state.request_id, type(exc).__name__)
        raise HTTPException(503, ("reco_unavailable", "El servicio de recomendación no está disponible")) from exc
    return resp.json()


# ----------------------------------------------------------------------------- insights


def resolve_range(request: Request, start: date | None, end: date | None) -> tuple[date, date]:
    lo, hi = request.app.state.components.analytics.date_range()
    end = end or hi
    start = start or (end - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if start > end:
        raise HTTPException(422, ("invalid_range", "'from' debe ser anterior o igual a 'to'"))
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(422, ("invalid_range", f"el rango máximo es de {MAX_RANGE_DAYS} días"))
    return max(start, lo), min(end, hi)


async def cached(request: Request, key: tuple, fn):
    """Caché de 10 min por réplica: con Athena cada consulta tarda segundos y cuesta por dato escaneado."""
    cache = request.app.state.insights_cache
    hit = cache.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    value = await run_in_threadpool(fn)
    cache[key] = (time.monotonic() + 600, value)
    return value


@app.get("/v1/insights/kpis", tags=["insights"], responses=ERRORS)
async def kpis(
    request: Request,
    start: date | None = Query(None, alias="from", description="Inicio (YYYY-MM-DD). Por defecto: 14 días antes de 'to'"),
    end: date | None = Query(None, alias="to", description="Fin (YYYY-MM-DD). Por defecto: último día con datos"),
):
    """KPIs del embudo vista → carrito → compra en el periodo, con el detalle diario.
    `new_visitor_share` es la proporción de visitantes sin historial (casos cold-start)."""
    start, end = resolve_range(request, start, end)
    analytics = request.app.state.components.analytics
    daily = await cached(request, ("kpis", start, end), lambda: analytics.daily_kpis(start, end))
    return {"from": str(start), "to": str(end), "summary": kpi_summary(daily), "daily": daily_rows(daily)}


@app.get("/v1/insights/promotions", tags=["insights"], responses=ERRORS)
async def promotions(
    request: Request,
    category_id: int | None = Query(None, description="Filtra por categoría (hoja)"),
    limit: int = Query(10, ge=1, le=50),
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
):
    """Acciones comerciales sugeridas por producto (`destacar` o `promocionar_descuento`), con el
    motivo en lenguaje de negocio. Solo considera productos con stock."""
    start, end = resolve_range(request, start, end)
    comps = request.app.state.components
    stats = await cached(request, ("items", start, end), lambda: comps.analytics.item_stats(start, end, min_views=20))
    days = (end - start).days + 1
    items = suggest_promotions(stats, comps.catalog, days, category_id=category_id, limit=limit)
    return {"from": str(start), "to": str(end), "items": items}


# ----------------------------------------------------------------------------- reportes


ReportType = Enum("ReportType", {t: t for t in REPORT_TYPES}, type=str)


class ReportParams(BaseModel):
    from_: date | None = Field(None, alias="from")
    to: date | None = None
    category_id: int | None = None
    limit: int = Field(100, ge=1, le=10_000)
    force_error: bool = Field(False, description="Solo pruebas: provoca fallas para ejercitar reintentos y DLQ")

    model_config = {"populate_by_name": True}


class ReportRequest(BaseModel):
    report_type: ReportType
    params: ReportParams = Field(default_factory=ReportParams)

    @model_validator(mode="after")
    def check_range(self):
        if self.params.from_ and self.params.to and self.params.from_ > self.params.to:
            raise ValueError("params.from debe ser anterior o igual a params.to")
        return self


class JobView(BaseModel):
    job_id: str
    status: Literal["queued", "running", "retrying", "done", "failed"]
    report_type: str
    params: dict
    created_at: str
    updated_at: str | None = None
    attempts: int = 0
    rows: int | None = None
    error: str | None = None
    download_url: str | None = None


def job_view(request: Request, job: dict) -> JobView:
    view = JobView(**{k: v for k, v in job.items() if k in JobView.model_fields})
    if job.get("status") == "done" and job.get("location"):
        view.download_url = request.app.state.components.reports.link(job["job_id"], job["location"])
    return view


@app.post("/v1/reports", tags=["reportes"], status_code=202, response_model=JobView, responses=ERRORS)
async def create_report(
    body: ReportRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=100),
    client_id: str = Header("local", alias="x-api-key-id", max_length=100),
):
    """Encola un reporte y responde de inmediato `202 Accepted` con el `job_id`.
    Con `Idempotency-Key`, repetir la misma petición devuelve el trabajo ya creado (no lo duplica)."""
    if body.params.force_error and not fault_injection_allowed():
        raise HTTPException(422, ("fault_injection_disabled", "force_error solo está habilitado en pruebas"))
    comps = request.app.state.components
    if idempotency_key:
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:{idempotency_key}"))
        existing = comps.jobs.get(job_id)
        if existing:
            response.status_code = 200
            return job_view(request, existing)
    else:
        job_id = str(uuid.uuid4())

    start, end = resolve_range(request, body.params.from_, body.params.to)
    params = body.params.model_dump(by_alias=True, exclude={"from_", "to"}) | {"from": str(start), "to": str(end)}
    job = {
        "job_id": job_id,
        "client_id": client_id,
        "report_type": body.report_type.value,
        "params": params,
        "status": "queued",
        "attempts": 0,
        "created_at": now(),
        "request_id": request.state.request_id,
    }
    comps.jobs.create(job)
    message = {k: job[k] for k in ("job_id", "client_id", "report_type", "params", "request_id")} | {"requested_at": job["created_at"]}
    try:
        await run_in_threadpool(request.app.state.publisher.publish_job, message)
    except BrokerUnavailable as exc:
        comps.jobs.update(job_id, status="failed", error="broker_unavailable")
        log.error('{"request_id":"%s","error":"broker_unavailable","detail":"%s"}', request.state.request_id, str(exc)[:200])
        raise HTTPException(503, ("broker_unavailable", "No se pudo encolar el reporte; reintente más tarde")) from exc
    response.headers["Location"] = f"/v1/reports/{job_id}"
    return job_view(request, job)


@app.get("/v1/reports/{job_id}", tags=["reportes"], response_model=JobView, responses={404: {"model": ErrorResponse}})
async def get_report(job_id: str, request: Request):
    """Estado del reporte. Cuando está `done`, incluye `download_url` (en AWS, URL prefirmada de S3 válida 15 min)."""
    job = request.app.state.components.jobs.get(job_id)
    if not job:
        raise HTTPException(404, ("not_found", "No existe el reporte"))
    return job_view(request, job)


@app.get("/v1/reports/{job_id}/download", tags=["reportes"], include_in_schema=False)
async def download_report(job_id: str, request: Request):
    store = request.app.state.components.reports
    job = request.app.state.components.jobs.get(job_id)
    if not isinstance(store, LocalReportStore) or not job or job.get("status") != "done":
        raise HTTPException(404, ("not_found", "No existe el reporte"))
    return FileResponse(store.path(job_id), media_type="text/csv", filename=f"{job['report_type']}_{job_id[:8]}.csv")
