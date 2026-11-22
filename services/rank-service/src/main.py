"""rank-service: reglas de negocio (stock, duplicados, diversidad) y orden final.

Invoca el endpoint real-time de SageMaker para puntuar candidatos.
"""
from fastapi import FastAPI

app = FastAPI(title="rank-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "rank-service"}

# TODO: POST /v1/rank  (candidatos -> SageMaker -> reglas -> top-N)
