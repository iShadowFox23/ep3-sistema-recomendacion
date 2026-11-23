"""Pruebas de la API empresarial: predicción, insights, reportes (CP-11 parcial) y contrato OpenAPI."""
import httpx
import pytest

from src.main import app


def test_predict_forwards_to_reco_service(client, reco_calls):
    resp = client.post(
        "/v1/recommendations/predict",
        json={"user_id": 7, "context": "pdp", "recent_items": [5, 6], "k": 3},
        headers={"x-request-id": "r-1"},
    )
    assert resp.status_code == 200 and resp.json()["strategy"] == "als"
    sent = reco_calls[0]
    assert sent.url.path == "/v1/recommendations"
    assert sent.url.params.get_list("item_id") == ["5", "6"] and sent.url.params["user_id"] == "7"
    assert sent.headers["x-request-id"] == "r-1"


def test_predict_returns_503_when_reco_is_down(client):
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    app.state.http = httpx.AsyncClient(base_url="http://reco", transport=httpx.MockTransport(down))
    resp = client.post("/v1/recommendations/predict", json={})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "reco_unavailable"


def test_predict_validation_error(client):
    resp = client.post("/v1/recommendations/predict", json={"context": "checkout"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_request"


def test_kpis_default_range_is_last_14_days(client):
    body = client.get("/v1/insights/kpis").json()
    assert (body["from"], body["to"]) == ("2015-09-07", "2015-09-20")
    s = body["summary"]
    assert s["days"] == 14 and s["visitors"] == 1400
    assert s["view_to_cart_rate"] == pytest.approx(6 / 150, abs=1e-4)
    assert s["conversion_rate"] == pytest.approx(0.02)
    assert s["new_visitor_share"] == pytest.approx(0.8)
    assert len(body["daily"]) == 14


def test_kpis_invalid_range(client):
    resp = client.get("/v1/insights/kpis", params={"from": "2015-09-10", "to": "2015-09-01"})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_range"


def test_promotions_are_prescriptive_and_only_in_stock(client):
    items = client.get("/v1/insights/promotions").json()["items"]
    actions = {i["item_id"]: i["action"] for i in items}
    assert actions == {1: "destacar", 3: "promocionar_descuento"}  # 4 no tiene stock, 2 no tiene señal
    assert all(i["reason"] for i in items)


def test_create_report_returns_202_and_enqueues(client, components):
    resp = client.post("/v1/reports", json={"report_type": "kpi_summary", "params": {"from": "2015-09-01", "to": "2015-09-05"}})
    assert resp.status_code == 202
    job = resp.json()
    assert job["status"] == "queued" and resp.headers["location"] == f"/v1/reports/{job['job_id']}"
    message = app.state.publisher.messages[0]
    assert message["job_id"] == job["job_id"] and message["params"]["from"] == "2015-09-01"
    assert components.jobs.get(job["job_id"])["status"] == "queued"


def test_idempotency_key_does_not_duplicate_jobs(client):
    headers = {"Idempotency-Key": "pedido-123"}
    first = client.post("/v1/reports", json={"report_type": "kpi_summary"}, headers=headers)
    second = client.post("/v1/reports", json={"report_type": "kpi_summary"}, headers=headers)
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(app.state.publisher.messages) == 1


def test_create_report_returns_503_and_marks_failed_when_broker_down(client, components):
    from tests.conftest import FakePublisher

    app.state.publisher = FakePublisher(fail=True)
    resp = client.post("/v1/reports", json={"report_type": "kpi_summary"})
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "broker_unavailable"


def test_unknown_report_type_and_disabled_fault_injection(client):
    assert client.post("/v1/reports", json={"report_type": "otro"}).status_code == 422
    resp = client.post("/v1/reports", json={"report_type": "kpi_summary", "params": {"force_error": True}})
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "fault_injection_disabled"


def test_get_unknown_report_is_404(client):
    resp = client.get("/v1/reports/no-existe")
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "not_found"


def test_openapi_documents_all_business_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/v1/recommendations/predict", "/v1/insights/kpis", "/v1/insights/promotions", "/v1/reports", "/v1/reports/{job_id}"):
        assert path in paths


def test_predict_forwards_cache_control_to_reco(client, reco_calls):
    client.post("/v1/recommendations/predict", json={"user_id": 7}, headers={"Cache-Control": "no-cache"})
    assert reco_calls[-1].headers["cache-control"] == "no-cache"
