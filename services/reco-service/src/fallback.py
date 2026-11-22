"""Fallback Handler: respuesta de respaldo cuando rank-service o SageMaker no responden a tiempo.

Usa `popular.json` (populares con stock, globales y por categoría raíz), generado por
`ml/export_serving_data.py`. Si ni siquiera eso está disponible, devuelve una lista vacía
marcada como degradada: el widget simplemente no se muestra, pero la tienda no se rompe.
"""
from __future__ import annotations

import json

from .storage import read_text


class Fallback:
    def __init__(self, popular: dict | None = None):
        popular = popular or {}
        self.global_items: list[int] = popular.get("global", [])
        self.by_category: dict[str, list[int]] = popular.get("by_category", {})

    @classmethod
    def load(cls, path: str | None) -> "Fallback":
        if not path:
            return cls()
        return cls(json.loads(read_text(path)))

    def recommend(self, k: int, category_id: int | None, exclude: set[int]) -> tuple[list[dict], str]:
        source = self.by_category.get(str(category_id)) if category_id is not None else None
        strategy = "fallback_popular_category" if source else "fallback_popular"
        pool = (source or []) + [i for i in self.global_items if i not in set(source or [])]
        picked = [i for i in pool if i not in exclude][:k]
        items = [{"item_id": i, "score": None, "reason": {"type": strategy}} for i in picked]
        return items, strategy
