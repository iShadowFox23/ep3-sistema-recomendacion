"""Cliente de business-api para el dashboard (sin dependencias de Streamlit, para poder probarlo)."""
from __future__ import annotations

import httpx


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class BusinessApi:
    def __init__(self, base_url: str, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        headers = {"x-api-key": api_key} if api_key else {}
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=30.0, transport=transport)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, "unreachable", f"No se pudo conectar con business-api ({type(exc).__name__})") from exc
        if resp.status_code >= 400:
            try:
                err = resp.json()["error"]
                raise ApiError(resp.status_code, err.get("code", "error"), err.get("message", ""))
            except (ValueError, KeyError, TypeError):
                raise ApiError(resp.status_code, "error", resp.text[:200]) from None
        return resp.json()

    def kpis(self, start: str | None = None, end: str | None = None) -> dict:
        params = {k: v for k, v in {"from": start, "to": end}.items() if v}
        return self._request("GET", "/v1/insights/kpis", params=params)

    def promotions(self, category_id: int | None = None, limit: int = 20, start: str | None = None, end: str | None = None) -> dict:
        params = {"limit": limit, "category_id": category_id, "from": start, "to": end}
        return self._request("GET", "/v1/insights/promotions", params={k: v for k, v in params.items() if v is not None})

    def predict(self, user_id: int | None, context: str, k: int, recent_items: list[int], category_id: int | None) -> dict:
        body = {"user_id": user_id, "context": context, "k": k, "recent_items": recent_items, "category_id": category_id}
        return self._request("POST", "/v1/recommendations/predict", json={k_: v for k_, v in body.items() if v is not None})

    def create_report(self, report_type: str, params: dict) -> dict:
        return self._request("POST", "/v1/reports", json={"report_type": report_type, "params": params})

    def report(self, job_id: str) -> dict:
        return self._request("GET", f"/v1/reports/{job_id}")

    def download(self, url: str) -> bytes:
        """URL relativa (modo local, la sirve business-api) o absoluta (URL prefirmada de S3)."""
        if url.startswith("/"):
            resp = self._http.get(url)
        else:
            resp = httpx.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.content


def parse_items(text: str) -> list[int]:
    """'355908, 12 13' -> [355908, 12, 13]; ignora lo que no sea número."""
    return [int(tok) for tok in text.replace(",", " ").split() if tok.strip().isdigit()]
