"""Pruebas unitarias del núcleo del recomendador (no requieren el dataset ni `implicit`)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ML = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ML))
sys.path.insert(0, str(ML / "inference"))

from inference import BadRequest, recommend  # noqa: E402
from recsys import Recommender, build_matrix, build_recommender, catalog_coverage, popularity_scores, ranking_metrics  # noqa: E402


@pytest.fixture()
def interactions():
    # 3 usuarios con >=2 productos, 1 usuario con un solo producto (queda fuera del modelo)
    return pd.DataFrame(
        {
            "visitorid": [1, 1, 2, 2, 2, 3, 3, 4],
            "itemid": [10, 11, 10, 12, 13, 11, 13, 14],
            "weight": [1, 5, 1, 1, 3, 1, 1, 1],
            "last_ts": [1, 2, 3, 4, 5, 6, 7, 8],
        }
    )


@pytest.fixture()
def recommender(interactions):
    data = build_matrix(interactions, min_user_items=2)
    rng = np.random.default_rng(0)
    user_factors = rng.normal(size=(len(data.user_ids), 4)).astype(np.float32)
    item_factors = rng.normal(size=(len(data.item_ids), 4)).astype(np.float32)
    item_factors[-1] = 0.0  # producto 14: solo lo vio el usuario 4 (fuera del modelo) -> sin señal, como en ALS real
    items = pd.DataFrame({"itemid": [10, 11, 12, 13, 14], "root_category_id": [1, 1, 2, 2, 2]})
    popularity = popularity_scores(interactions, data.item_ids, recent_days=10_000)
    return build_recommender(data, user_factors, item_factors, popularity, items, {"model": "test"})


def test_build_matrix_filters_single_item_users(interactions):
    data = build_matrix(interactions, min_user_items=2)
    assert list(data.user_ids) == [1, 2, 3]
    assert list(data.item_ids) == [10, 11, 12, 13, 14]  # todos los productos se conservan
    assert data.matrix.shape == (3, 5)
    assert data.matrix[0, 1] == 5  # usuario 1, producto 11


def test_recommend_excludes_seen_items(recommender):
    row = recommender.user_row(2)
    recs = recommender.recommend_batch(np.array([row]), k=2)[0]
    seen = set(recommender.history[row].indices.tolist())
    assert not seen & set(recs.tolist())


def test_save_and_load_roundtrip(recommender, tmp_path):
    recommender.save(tmp_path)
    loaded = Recommender.load(tmp_path)
    np.testing.assert_allclose(loaded.item_factors, recommender.item_factors)
    assert (loaded.history != recommender.history).nnz == 0
    assert loaded.metadata["model"] == "test"


def test_ranking_metrics_known_values():
    recs = np.array([[0, 1, 2], [3, 4, 5]])
    relevant = [{1, 9}, {7}]
    m = ranking_metrics(recs, relevant, n_items=10, k=3)
    assert m["precision@3"] == pytest.approx((1 / 3 + 0) / 2)
    assert m["recall@3"] == pytest.approx((1 / 2 + 0) / 2)
    assert m["hitrate@3"] == pytest.approx(0.5)
    assert m["users_evaluated"] == 2
    # (10-2-1)/10 y (10-3-1)/10: con 10 productos ya es 0,65 acertando casi nada;
    # con 215.000 productos tiende a 0,9999 -> por eso accuracy es engañosa aquí
    assert m["accuracy@3"] == pytest.approx((0.7 + 0.6) / 2)


def test_catalog_coverage():
    assert catalog_coverage(np.array([[0, 1], [1, 2]]), n_items=10) == pytest.approx(0.3)


# ---- inferencia (CP-04: cold-start, CP-05: respuesta con explicación)


def test_inference_known_user_has_explanations(recommender):
    out = recommend(recommender, {"user_id": 1, "context": "home", "k": 2})
    assert out["strategy"] == "als" and out["cold_start"] is False
    assert len(out["items"]) == 2
    for item in out["items"]:
        assert item["item_id"] not in (10, 11)  # ya vistos por el usuario 1
        assert item["reason"]["type"] == "similar_to_history"
        assert item["reason"]["item_id"] in (10, 11)


def test_inference_cold_start_uses_category_popularity(recommender):
    out = recommend(recommender, {"user_id": 999, "category_id": 2, "k": 2})
    assert out["cold_start"] is True and out["strategy"] == "popular_category"
    assert {i["item_id"] for i in out["items"]} <= {12, 13, 14}


def test_inference_pdp_uses_current_item(recommender):
    out = recommend(recommender, {"user_id": 999, "context": "pdp", "recent_items": [12], "k": 3})
    assert out["strategy"] == "item_similarity"
    assert 12 not in {i["item_id"] for i in out["items"]}


def test_inference_pdp_item_without_signal_falls_back_to_its_category(recommender):
    out = recommend(recommender, {"context": "pdp", "recent_items": [14], "k": 2})
    assert out["strategy"] == "popular_category" and out["cold_start"] is True
    ids = {i["item_id"] for i in out["items"]}
    assert 14 not in ids and ids <= {12, 13}


@pytest.mark.parametrize(
    "bad",
    [{"context": "checkout"}, {"k": 0}, {"k": 500}, {"user_id": "abc"}, {"recent_items": "12"}],
)
def test_inference_rejects_invalid_requests(recommender, bad):
    with pytest.raises(BadRequest):
        recommend(recommender, bad)
