"""Reglas de negocio aplicadas sobre los candidatos del modelo (parte prescriptiva del sistema).

Orden de aplicación (cada regla registra cuántos productos descartó, para que la decisión sea
explicable en el dashboard):
1. Duplicados: un producto aparece una sola vez.
2. Contexto: no se recomienda el producto que el usuario está viendo ni lo que ya está en el carrito.
3. Stock: se descartan productos sin disponibilidad (si se desconoce el stock, se conserva).
4. Diversidad: como máximo `max_per_category` productos por categoría (si se desconoce, no limita).
   Es una regla blanda: si después de filtrar faltan productos para completar `k`, se rellena con
   los descartados por diversidad (en el orden del modelo). Stock y contexto son reglas estrictas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Catalog


@dataclass
class RuleStats:
    removed_duplicates: int = 0
    removed_in_context: int = 0
    removed_unavailable: int = 0
    removed_diversity: int = 0
    refilled_diversity: int = 0  # productos sobre el tope de su categoría usados para completar k

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class RuleConfig:
    filter_unavailable: bool = True
    max_per_category: int = 3


@dataclass
class RankResult:
    items: list[dict]
    stats: RuleStats = field(default_factory=RuleStats)


def apply_rules(candidates: list[dict], catalog: Catalog, k: int, exclude: set[int], config: RuleConfig) -> RankResult:
    stats = RuleStats()
    seen: set[int] = set()
    per_category: dict[int, int] = {}
    kept: list[dict] = []
    over_cap: list[dict] = []  # descartados solo por diversidad, por si hay que rellenar
    for cand in candidates:
        item_id = int(cand["item_id"])
        if item_id in seen:
            stats.removed_duplicates += 1
            continue
        seen.add(item_id)
        if item_id in exclude:
            stats.removed_in_context += 1
            continue
        info = catalog.get(item_id)
        if config.filter_unavailable and info.available is False:
            stats.removed_unavailable += 1
            continue
        if info.category_id is not None:
            if per_category.get(info.category_id, 0) >= config.max_per_category:
                over_cap.append({**cand, "category_id": info.category_id, "available": info.available})
                continue
            per_category[info.category_id] = per_category.get(info.category_id, 0) + 1
        kept.append({**cand, "category_id": info.category_id, "available": info.available})
        if len(kept) == k:
            break
    refill = over_cap[: k - len(kept)]
    stats.removed_diversity = len(over_cap) - len(refill)
    stats.refilled_diversity = len(refill)
    return RankResult(items=kept + refill, stats=stats)
