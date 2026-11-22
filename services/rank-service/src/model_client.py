"""Clientes del modelo: endpoint real-time de SageMaker o un stub para desarrollo local."""
from __future__ import annotations

import json
from typing import Protocol


class ModelUnavailable(RuntimeError):
    """El modelo no respondió (timeout, error del endpoint o de red)."""


class ModelClient(Protocol):
    def predict(self, request: dict) -> dict: ...


class SageMakerModelClient:
    def __init__(self, endpoint_name: str, region: str | None = None, timeout_s: float = 1.0):
        import boto3
        from botocore.config import Config

        self.endpoint_name = endpoint_name
        self._runtime = boto3.client(
            "sagemaker-runtime",
            region_name=region,
            config=Config(connect_timeout=timeout_s, read_timeout=timeout_s, retries={"max_attempts": 1}),
        )

    def predict(self, request: dict) -> dict:
        try:
            resp = self._runtime.invoke_endpoint(
                EndpointName=self.endpoint_name,
                ContentType="application/json",
                Accept="application/json",
                Body=json.dumps(request),
            )
            return json.loads(resp["Body"].read())
        except Exception as exc:  # botocore lanza varios tipos; todos significan "sin modelo"
            raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc


class StubModelClient:
    """Respuesta determinista sin AWS (desarrollo local y pruebas)."""

    def __init__(self, item_ids: list[int] | None = None):
        self.item_ids = item_ids or list(range(1000, 1100))

    def predict(self, request: dict) -> dict:
        k = request.get("k", 10)
        exclude = set(request.get("recent_items") or [])
        items = [i for i in self.item_ids if i not in exclude][:k]
        return {
            "items": [
                {"item_id": i, "score": round(1.0 - n / 100, 4), "reason": {"type": "stub"}} for n, i in enumerate(items)
            ],
            "strategy": "stub",
            "cold_start": False,
            "model": {"model": "stub"},
        }
