"""Handlers del endpoint real-time de SageMaker (contenedor SKLearn: numpy/scipy/pandas).

El `model.tar.gz` contiene `model.npz`, `metadata.json` y `code/` (este archivo + recsys.py).

Petición (application/json):
    {"user_id": 257597, "context": "home|pdp|cart", "k": 10,
     "recent_items": [355908, ...],   # opcional: productos de la sesión / ficha / carrito
     "category_id": 1532}             # opcional: categoría raíz para cold-start

Respuesta:
    {"items": [{"item_id": 1, "score": 0.83,
                "reason": {"type": "similar_to_history", "item_id": 355908, "similarity": 0.91}}],
     "strategy": "als | item_similarity | popular_category | popular",
     "cold_start": false, "model": {...}}

Estrategia (de más a menos personalizada):
1. context pdp/cart con `recent_items` conocidos -> productos parecidos a esos (contextual).
2. usuario conocido por el modelo                -> ALS (factores latentes), sin productos ya vistos.
3. usuario desconocido con `recent_items`       -> productos parecidos a su sesión.
4. sin historial (cold-start)                    -> populares de su categoría o populares globales.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recsys import Recommender  # noqa: E402

VALID_CONTEXTS = {"home", "pdp", "cart"}
MAX_K = 50


class BadRequest(ValueError):
    pass


def model_fn(model_dir: str) -> Recommender:
    return Recommender.load(Path(model_dir))


def input_fn(body, content_type: str = "application/json") -> dict:
    if content_type != "application/json":
        raise BadRequest(f"content type no soportado: {content_type}")
    request = json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
    if not isinstance(request, dict):
        raise BadRequest("el cuerpo debe ser un objeto JSON")
    return request


def predict_fn(request: dict, rec: Recommender) -> dict:
    return recommend(rec, request)


def output_fn(prediction: dict, accept: str = "application/json"):
    return json.dumps(prediction), "application/json"


def _validate(request: dict) -> tuple[int | None, str, int, list[int], int | None]:
    context = request.get("context", "home")
    if context not in VALID_CONTEXTS:
        raise BadRequest(f"context debe ser uno de {sorted(VALID_CONTEXTS)}")
    k = request.get("k", 10)
    if not isinstance(k, int) or not 1 <= k <= MAX_K:
        raise BadRequest(f"k debe ser un entero entre 1 y {MAX_K}")
    user_id = request.get("user_id")
    if user_id is not None and not isinstance(user_id, int):
        raise BadRequest("user_id debe ser entero")
    recent = request.get("recent_items") or []
    if not isinstance(recent, list) or not all(isinstance(i, int) for i in recent):
        raise BadRequest("recent_items debe ser una lista de enteros")
    category = request.get("category_id")
    return user_id, context, k, recent, category


def recommend(rec: Recommender, request: dict) -> dict:
    user_id, context, k, recent, category = _validate(request)
    known_recent = rec.item_rows(recent)
    recent_rows = rec.with_signal(known_recent)  # solo productos con vector latente útil
    user_row = rec.user_row(user_id) if user_id is not None else None
    history_rows = rec.history[user_row].indices if user_row is not None else recent_rows
    if category is None and len(known_recent) > 0 and len(recent_rows) == 0:
        # Producto conocido pero sin señal colaborativa: usar su categoría para el respaldo.
        category = int(rec.item_category[known_recent[-1]])

    if context in ("pdp", "cart") and len(recent_rows) > 0:
        strategy = "item_similarity"
        exclude = set(history_rows.tolist()) | set(recent_rows.tolist())
        candidates = rec.similar_to_items(recent_rows, k + len(exclude))
        rows = np.array([i for i in candidates if i not in exclude][:k], dtype=np.int64)
        scores = rec._item_unit[rows] @ rec._item_unit[recent_rows].mean(axis=0)
    elif user_row is not None:
        strategy = "als"
        rows = rec.recommend_batch(np.array([user_row]), k)[0]
        scores = rec.item_factors[rows] @ rec.user_factors[user_row]
    elif len(recent_rows) > 0:
        strategy = "item_similarity"
        rows = rec.similar_to_items(recent_rows, k)
        scores = rec._item_unit[rows] @ rec._item_unit[recent_rows].mean(axis=0)
    else:
        known_category = category is not None and bool((rec.item_category == category).any())
        strategy = "popular_category" if known_category else "popular"
        rows = rec.popular_in_category(category if known_category else None, k, exclude=set(known_recent.tolist()))
        scores = rec.popularity[rows] / max(float(rec.popularity.max()), 1e-8)

    items = []
    for row, score in zip(rows, scores):
        if strategy in ("als", "item_similarity"):
            source, sim = rec.explain(history_rows, int(row))
            reason = {"type": "similar_to_history", "item_id": source, "similarity": round(sim, 4)}
        else:
            reason = {"type": strategy}
        items.append({"item_id": int(rec.item_ids[row]), "score": round(float(score), 6), "reason": reason})

    return {
        "items": items,
        "strategy": strategy,
        "cold_start": strategy.startswith("popular"),
        "model": {k_: rec.metadata.get(k_) for k_ in ("model", "params", "train_splits")},
    }
