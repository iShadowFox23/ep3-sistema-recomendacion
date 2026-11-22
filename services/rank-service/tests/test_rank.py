"""Pruebas de rank-service: reglas de negocio (CP-02, CP-03) y API con un modelo falso."""
import pytest
from fastapi.testclient import TestClient

from src.catalog import Catalog, ItemInfo
from src.main import app
from src.model_client import ModelUnavailable, StubModelClient
from src.rules import RuleConfig, apply_rules

CATALOG = Catalog(
    {
        1: ItemInfo(category_id=10, available=True),
        2: ItemInfo(category_id=10, available=False),  # sin stock
        3: ItemInfo(category_id=10, available=True),
        4: ItemInfo(category_id=10, available=True),
        5: ItemInfo(category_id=10, available=True),  # 4.º de la categoría 10
        6: ItemInfo(category_id=20, available=True),
        7: ItemInfo(category_id=None, available=None),  # sin datos: no se filtra
    }
)


def cands(*ids):
    return [{"item_id": i, "score": 1.0 - n / 10} for n, i in enumerate(ids)]


# ---------------------------------------------------------------- reglas


def test_cp02_removes_unavailable_and_duplicates():
    result = apply_rules(cands(1, 1, 2, 3), CATALOG, k=10, exclude=set(), config=RuleConfig())
    assert [i["item_id"] for i in result.items] == [1, 3]
    assert result.stats.removed_duplicates == 1
    assert result.stats.removed_unavailable == 1


def test_cp03_diversity_caps_items_per_category():
    result = apply_rules(cands(1, 3, 4, 5, 6), CATALOG, k=4, exclude=set(), config=RuleConfig(max_per_category=3))
    assert [i["item_id"] for i in result.items] == [1, 3, 4, 6]  # el 5 (4.º de la categoría 10) queda fuera
    assert result.stats.removed_diversity == 1


def test_diversity_is_soft_when_not_enough_candidates():
    # solo hay 4 candidatos válidos y todos de la categoría 10: se completa k con el que superaba el tope
    result = apply_rules(cands(1, 3, 4, 5), CATALOG, k=4, exclude=set(), config=RuleConfig(max_per_category=3))
    assert [i["item_id"] for i in result.items] == [1, 3, 4, 5]
    assert result.stats.refilled_diversity == 1 and result.stats.removed_diversity == 0


def test_excludes_items_in_context_and_keeps_unknown_items():
    result = apply_rules(cands(6, 7, 1), CATALOG, k=10, exclude={6}, config=RuleConfig())
    assert [i["item_id"] for i in result.items] == [7, 1]
    assert result.stats.removed_in_context == 1


def test_stops_at_k_and_keeps_model_order():
    result = apply_rules(cands(6, 1, 3, 4), CATALOG, k=2, exclude=set(), config=RuleConfig())
    assert [i["item_id"] for i in result.items] == [6, 1]


def test_stock_filter_can_be_disabled():
    result = apply_rules(cands(2), CATALOG, k=5, exclude=set(), config=RuleConfig(filter_unavailable=False))
    assert [i["item_id"] for i in result.items] == [2]


# ---------------------------------------------------------------- API


class FailingModel:
    def predict(self, request):
        raise ModelUnavailable("timeout")


class RecordingModel(StubModelClient):
    def __init__(self):
        super().__init__([1, 2, 3, 4, 5, 6, 7])
        self.last_request = None

    def predict(self, request):
        self.last_request = request
        return super().predict(request)


@pytest.fixture()
def client():
    app.state.model = RecordingModel()
    app.state.catalog = CATALOG
    app.state.rules = RuleConfig()
    with TestClient(app) as c:
        yield c
    for attr in ("model", "catalog", "rules"):
        delattr(app.state, attr)


def test_rank_applies_rules_and_reports_them(client):
    resp = client.post("/v1/rank", json={"user_id": 1, "k": 3}, headers={"x-request-id": "abc"})
    assert resp.status_code == 200
    body = resp.json()
    assert [i["item_id"] for i in body["items"]] == [1, 3, 4]
    assert body["rules"]["removed_unavailable"] == 1
    assert resp.headers["x-request-id"] == "abc"
    # pide más candidatos de los que devuelve, para compensar los filtros
    assert app.state.model.last_request["k"] == 12


def test_rank_returns_503_when_model_unavailable(client):
    app.state.model = FailingModel()
    resp = client.post("/v1/rank", json={"user_id": 1})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "model_unavailable"


@pytest.mark.parametrize("bad", [{"context": "checkout"}, {"k": 0}, {"k": 21}, {"recent_items": "x"}])
def test_rank_validation_error_uses_common_format(client, bad):
    resp = client.post("/v1/rank", json=bad)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
