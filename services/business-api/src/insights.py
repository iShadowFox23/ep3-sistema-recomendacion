"""Lógica de negocio de los insights: resumen de KPIs y promociones sugeridas (prescriptivo)."""
from __future__ import annotations

import pandas as pd

from .catalog import Catalog


def _rate(num: float, den: float) -> float | None:
    return round(num / den, 4) if den else None


def kpi_summary(daily: pd.DataFrame) -> dict:
    """Totales y tasas del periodo.

    - view_to_cart_rate      = carritos / vistas
    - cart_to_purchase_rate  = compras / carritos
    - conversion_rate        = compradores / visitantes (suma de valores diarios)
    - new_visitor_share      = visitantes nuevos / visitantes: proporción de casos cold-start
    """
    t = daily[["visitors", "new_visitors", "buyers", "views", "carts", "transactions"]].sum()
    return {
        "days": int(len(daily)),
        "visitors": int(t["visitors"]),
        "views": int(t["views"]),
        "carts": int(t["carts"]),
        "transactions": int(t["transactions"]),
        "view_to_cart_rate": _rate(t["carts"], t["views"]),
        "cart_to_purchase_rate": _rate(t["transactions"], t["carts"]),
        "conversion_rate": _rate(t["buyers"], t["visitors"]),
        "new_visitor_share": _rate(t["new_visitors"], t["visitors"]),
    }


def daily_rows(daily: pd.DataFrame) -> list[dict]:
    rows = []
    for r in daily.itertuples(index=False):
        rows.append(
            {
                "date": str(r.date),
                "visitors": int(r.visitors),
                "views": int(r.views),
                "carts": int(r.carts),
                "transactions": int(r.transactions),
                "conversion_rate": _rate(r.buyers, r.visitors),
                "new_visitor_share": _rate(r.new_visitors, r.visitors),
            }
        )
    return rows


def suggest_promotions(
    stats: pd.DataFrame, catalog: Catalog, days: int, category_id: int | None = None, limit: int = 10, min_views: int = 20
) -> list[dict]:
    """Recomendación de acciones comerciales por producto, cruzando demanda observada y stock.

    Solo se consideran productos con stock y al menos `min_views` vistas en el periodo. La referencia
    es la tasa de conversión agregada de esos productos (compras totales / vistas totales); no se usa
    la mediana porque en e-commerce la mayoría de los productos no registra compras y la mediana es 0.

    - `destacar`:               convierte al menos el doble que la referencia -> darle más visibilidad
                                (home, primeras posiciones del widget).
    - `promocionar_descuento`:  está en el cuartil superior de vistas, tiene carritos, pero convierte
                                menos de la mitad que la referencia -> hay interés sin compra; un
                                descuento o envío gratis puede destrabarlo.

    Nota: el dataset no incluye margen ni precio, por lo que la regla no los considera.
    """
    if stats.empty:
        return []
    df = stats[stats["views"] >= min_views].copy()
    df["available"] = [catalog.get(int(i)).available for i in df["itemid"]]
    df["category_id"] = [catalog.get(int(i)).category_id for i in df["itemid"]]
    df = df[df["available"] == True]  # noqa: E712 (None = sin dato de stock: se excluye)
    if category_id is not None:
        df = df[df["category_id"] == category_id]
    if df.empty:
        return []

    df["conversion"] = df["transactions"] / df["views"]
    reference = float(df["transactions"].sum() / df["views"].sum())
    views_q75 = float(df["views"].quantile(0.75))
    feature = df[(df["transactions"] > 0) & (df["conversion"] >= max(2 * reference, 1e-9))].copy()
    feature["action"], feature["priority"] = "destacar", feature["views"] * feature["conversion"]
    discount = df[(df["views"] >= views_q75) & (df["carts"] > 0) & (df["conversion"] < reference / 2)].copy()
    discount["action"], discount["priority"] = "promocionar_descuento", discount["carts"]

    out = []
    for r in pd.concat([feature, discount]).sort_values("priority", ascending=False).head(limit).itertuples(index=False):
        if r.action == "destacar":
            reason = (
                f"{int(r.views)} vistas y {int(r.transactions)} compras en {days} días: convierte "
                f"{r.conversion:.1%}, más del doble de la referencia ({reference:.1%}). Tiene stock."
            )
        else:
            reason = (
                f"{int(r.views)} vistas y {int(r.carts)} carritos en {days} días, pero solo {int(r.transactions)} "
                f"compras ({r.conversion:.1%} vs referencia {reference:.1%}): interés sin conversión. Tiene stock."
            )
        out.append(
            {
                "item_id": int(r.itemid),
                "category_id": None if pd.isna(r.category_id) else int(r.category_id),
                "action": r.action,
                "views": int(r.views),
                "carts": int(r.carts),
                "transactions": int(r.transactions),
                "conversion_rate": round(float(r.conversion), 4),
                "reason": reason,
            }
        )
    return out
