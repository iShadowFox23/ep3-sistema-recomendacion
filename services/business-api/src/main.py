"""business-api: API REST empresarial expuesta vía API Gateway
(REST API + VPC Link + NLB + ALB). Swagger/OpenAPI en /docs.
"""
from fastapi import FastAPI

app = FastAPI(title="business-api", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "business-api"}

# TODO:
#   POST /v1/recommendations/predict
#   GET  /v1/insights/kpis
#   GET  /v1/insights/promotions
#   POST /v1/reports          -> 202 + job_id (publica en RabbitMQ)
#   GET  /v1/reports/{job_id}
