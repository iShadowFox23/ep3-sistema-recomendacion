"""Pruebas de reco-service: fallback (CP-01), autenticación, validación (CP-06), caché y telemetría."""
import httpx
import pytest
from fastapi.testclient import TestClient

from src.cache import MemoryCache
from src.fallback import Fallback
from src.main import app

POPULAR = {"global": [100, 101, 102, 103], "by_category": {"7": [200, 201]}}
RANKED = {
    "items": [{"item_id": 1, "score": 0.9, "reason": {"type": "similar_to_history", "item_id": 5, "similarity": 0.8}}],
    "strategy": "als",
    "cold_start": False,
    "rules": {"removed_unavailable": 2},
}


class FakeRank:
    """Simula rank-service: responde, demora (timeout) o falla."""

    def __init__(self, mode="ok"):
        self.mode, self.calls, self.last_json, self.last_headers = mode, 0, None, None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_json = request.read()
        self.last_headers = request.headers
        if self.mode == "timeout":
            raise httpx.ReadTimeout("rank-service no respondió en 150 ms", request=request)
        if self.mode == "error":
            return httpx.Response(503, json={"error": {"code": "model_unavailable"}})
        return httpx.Response(200, json=RANKED)


def make_client(rank: FakeRank, api_keys=frozenset()):
    app.state.cache = MemoryCache(ttl_s=60)
    app.state.fallback = Fallback(POPULAR)
    app.state.api_keys = set(api_keys)
    app.state.http = httpx.AsyncClient(base_url="http://rank", transport=httpx.MockTransport(rank.handler))
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    yield
    for attr in ("cache", "fallback", "api_keys", "http"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def test_returns_ranked_items_and_propagates_request_id():
    rank = FakeRank()
    with make_client(rank) as client:
        resp = client.get("/v1/recommendations", params={"user_id": 42, "context": "home"}, headers={"x-request-id": "req-1"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["strategy"] == "als" and body["degraded"] is False
    assert body["items"][0]["reason"]["type"] == "similar_to_history"
    assert resp.headers["x-request-id"] == "req-1" == rank.last_headers["x-request-id"]
    assert "x-response-time-ms" in resp.headers


def test_cp01_timeout_falls_back_to_popular_by_category():
    with make_client(FakeRank("timeout")) as client:
        resp = client.get("/v1/recommendations", params={"context": "pdp", "item_id": [200], "category_id": 7, "k": 3})
    body = resp.json()
    assert resp.status_code == 200  # el widget recibe respuesta aunque el modelo falle
    assert body["degraded"] is True and body["degraded_reason"] == "rank_timeout"
    assert body["strategy"] == "fallback_popular_category"
    assert [i["item_id"] for i in body["items"]] == [201, 100, 101]  # sin el producto que está viendo


def test_rank_error_falls_back_to_global_popular():
    with make_client(FakeRank("error")) as client:
        body = client.get("/v1/recommendations", params={"k": 2}).json()
    assert body["degraded_reason"] == "rank_error" and body["strategy"] == "fallback_popular"
    assert [i["item_id"] for i in body["items"]] == [100, 101]


def test_second_request_is_served_from_cache():
    rank = FakeRank()
    with make_client(rank) as client:
        first = client.get("/v1/recommendations", params={"user_id": 42}).json()
        second = client.get("/v1/recommendations", params={"user_id": 42}).json()
    assert (first["cache"], second["cache"]) == ("miss", "hit")
    assert rank.calls == 1


def test_degraded_responses_are_not_cached():
    rank = FakeRank("timeout")
    with make_client(rank) as client:
        client.get("/v1/recommendations", params={"user_id": 42})
        client.get("/v1/recommendations", params={"user_id": 42})
    assert rank.calls == 2


def test_missing_api_key_is_401_and_wrong_key_is_403():
    with make_client(FakeRank(), api_keys={"tienda-123"}) as client:
        missing = client.get("/v1/recommendations")
        wrong = client.get("/v1/recommendations", headers={"x-api-key": "otra"})
        ok = client.get("/v1/recommendations", headers={"x-api-key": "tienda-123"})
    assert missing.status_code == 401 and missing.json()["error"]["code"] == "missing_api_key"
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "invalid_api_key"
    assert ok.status_code == 200


@pytest.mark.parametrize("params", [{"context": "checkout"}, {"k": 0}, {"k": 50}, {"user_id": "abc"}])
def test_cp06_invalid_params_return_422_with_common_format(params):
    with make_client(FakeRank()) as client:
        resp = client.get("/v1/recommendations", params=params, headers={"x-request-id": "req-9"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "invalid_request" and error["request_id"] == "req-9"


def test_memory_cache_expires_entries():
    cache = MemoryCache(ttl_s=-1)
    cache.set("a", {"x": 1})
    assert cache.get("a") is None


def test_empty_ranking_is_backfilled_with_popular_in_stock():
    """Si las reglas de stock descartan todos los candidatos, el widget no queda vacío."""
    rank = FakeRank()
    empty = {"items": [], "strategy": "als", "cold_start": False, "rules": {"removed_unavailable": 12}}
    rank.handler = lambda request: httpx.Response(200, json=empty)
    with make_client(rank) as client:
        body = client.get("/v1/recommendations", params={"user_id": 42, "k": 3}).json()
    assert body["degraded"] is False and body["strategy"] == "als"
    assert body["backfilled"] == 3 and [i["item_id"] for i in body["items"]] == [100, 101, 102]
    assert all(i["reason"]["type"] == "backfill_popular" for i in body["items"])


def test_partial_ranking_is_topped_up_without_duplicates():
    rank = FakeRank()
    partial = {"items": [{"item_id": 100, "score": 0.9, "reason": {"type": "similar_to_history"}}], "strategy": "als", "cold_start": False}
    rank.handler = lambda request: httpx.Response(200, json=partial)
    with make_client(rank) as client:
        body = client.get("/v1/recommendations", params={"user_id": 42, "k": 3}).json()
    assert [i["item_id"] for i in body["items"]] == [100, 101, 102] and body["backfilled"] == 2


def test_cache_control_no_cache_forces_a_fresh_prediction():
    rank = FakeRank()
    with make_client(rank) as client:
        client.get("/v1/recommendations", params={"user_id": 42})
        fresh = client.get("/v1/recommendations", params={"user_id": 42}, headers={"Cache-Control": "no-cache"}).json()
        cached = client.get("/v1/recommendations", params={"user_id": 42}).json()
    assert fresh["cache"] == "miss" and cached["cache"] == "hit"
    assert rank.calls == 2
