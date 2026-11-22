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
| `services/report-worker/` | Consumidor de RabbitMQ que genera reportes batch (Athena → S3) |
| `dashboard/` | Dashboard Streamlit (KPIs, promociones, interpretabilidad) |
| `ml/` | Preparación de datos, entrenamiento, evaluación e inferencia de SageMaker |
| `infra/` | Scripts y plantillas de despliegue en AWS Academy |
| `tests/load/` | Pruebas de carga con Locust |
| `tests/postman/` | Colección Postman (pruebas de API y demo en vivo) |
| `data/` | Dataset (fuera de git, ver `data/README.md`) |
| `docs/`, `presentations/` | Informe acumulativo, guion y presentaciones |

## Ejecución local

```bash
docker compose up --build
```

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
