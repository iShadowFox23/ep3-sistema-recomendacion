# Sistema de Recomendación de Productos para E-commerce — EP3

ITY1102 Arquitectura de Sistemas de IA · Duoc UC · Equipo N.º 6

Sistema de recomendación para un retail de tecnología integrado con Shopify: modelo desplegado en
Amazon SageMaker, microservicios en ECS Fargate, API empresarial vía API Gateway, bus de mensajes
RabbitMQ y dashboard en Streamlit.

## Estructura

| Ruta | Contenido |
|---|---|
| `services/reco-service/` | API de recomendación para el widget (candidatos, caché, fallback, llamada a ranking) |
| `services/rank-service/` | Reglas de negocio y ranking; invoca el endpoint de SageMaker |
| `services/business-api/` | API empresarial (`/v1/recommendations/predict`, `/v1/insights/*`, `/v1/reports`) |
| `services/business-api/src/worker.py` | report-worker: consumidor de RabbitMQ que genera reportes batch (misma imagen que business-api, proceso aparte) |
| `dashboard/` | Dashboard Streamlit (KPIs, promociones, interpretabilidad) |
| `ml/` | Preparación de datos, entrenamiento, evaluación e inferencia de SageMaker |
| `infra/` | Scripts y plantillas de despliegue en AWS Academy |
| `tests/load/` | Pruebas de carga con Locust |
| `tests/postman/` | Colección Postman (pruebas de API y demo en vivo) |
| `data/` | Dataset (fuera de git, ver `data/README.md`) |
| `docs/`, `presentations/` | Informe acumulativo, guion y presentaciones |

## Ejecución local

```bash
python ml/export_serving_data.py              # catálogo y populares para los servicios (build/serving/)
python ml/export_analytics.py                 # KPIs diarios para business-api en modo local (build/analytics/)
docker compose up --build                     # MODEL_BACKEND=stub: sin AWS
MODEL_BACKEND=sagemaker docker compose up --build reco-service rank-service   # usa el endpoint real (~/.aws del lab)
curl -H "x-api-key: local-dev-key" "http://localhost:8080/v1/recommendations?user_id=257597&context=home&k=5"
```

### Flujo en línea

`widget → reco-service (auth, caché, fallback) → rank-service (reglas) → endpoint SageMaker reco-als`

- **reco-service** `GET /v1/recommendations?user_id&context=home|pdp|cart&k&item_id&category_id` (header `X-Api-Key`).
  Caché TTL 15 min (Redis si `REDIS_URL`, si no memoria). Si rank-service no responde en `RANK_TIMEOUT_MS`
  (150 ms en ECS), responde populares con stock (`degraded: true`).
- **rank-service** `POST /v1/rank`: pide k×4 candidatos al modelo y aplica reglas: duplicados, contexto,
  stock (estricta) y diversidad ≤3 por categoría (blanda: rellena si faltan productos). Devuelve cuántos
  productos descartó cada regla (`rules`).

### API empresarial (business-api, Swagger en http://localhost:8082/docs)

| Endpoint | Qué hace |
|---|---|
| `POST /v1/recommendations/predict` | Predicción en tiempo real con explicación (vía reco-service) |
| `GET /v1/insights/kpis?from&to` | Embudo vista → carrito → compra, conversión y % de visitantes nuevos (cold-start) |
| `GET /v1/insights/promotions?category_id&limit` | Prescriptivo: `destacar` / `promocionar_descuento` con motivo, solo productos con stock |
| `POST /v1/reports` | Encola un reporte en RabbitMQ → `202` + `job_id` (`Idempotency-Key` opcional) |
| `GET /v1/reports/{job_id}` | Estado y `download_url` (URL prefirmada S3 en AWS) |

Reportes: `kpi_summary`, `top_products`, `cold_start_coverage`. report-worker usa prefetch=1, ack manual,
3 intentos con backoff y luego `reports.dlq`. Backends: local (CSV + archivos) o AWS
(`ANALYTICS_BACKEND=athena`, `JOBS_TABLE=report_jobs`, `REPORTS_BUCKET=...`). Data lake: `python infra/scripts/datalake.py setup`.

- reco-service: http://localhost:8080/health
- rank-service: http://localhost:8081/health
- business-api: http://localhost:8082/docs (Swagger)
- dashboard: http://localhost:8501
- RabbitMQ management: http://localhost:15672 (guest/guest, solo local)

## Pruebas

```bash
cd services/<servicio>
pip install -r requirements.txt
python -m pytest tests
```
