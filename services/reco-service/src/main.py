"""reco-service: recomendaciones en línea para el widget de Shopify.

Componentes (C4 nivel 3): API Controller, Autenticación, Recuperador de candidatos,
Cliente de ranking, Fallback Handler (timeout 150 ms) y Telemetría.
"""
from fastapi import FastAPI

app = FastAPI(title="reco-service", version="0.1.0")


@app.get("/health")
def health():
    return {"status": "ok", "service": "reco-service"}

# TODO: GET /v1/recommendations?user_id=&context=home|pdp|cart
