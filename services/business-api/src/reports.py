"""Generación de reportes batch (la ejecuta report-worker, nunca la API)."""
from __future__ import annotations

from datetime import date

import pandas as pd

from .analytics import Analytics
from .catalog import Catalog
from .insights import daily_rows

REPORT_TYPES = ("kpi_summary", "top_products", "cold_start_coverage")


class FaultInjected(RuntimeError):
    """Falla provocada a propósito (solo con ALLOW_FAULT_INJECTION=true) para probar reintentos y DLQ."""


def generate(report_type: str, params: dict, analytics: Analytics, catalog: Catalog) -> pd.DataFrame:
    start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
    if report_type == "kpi_summary":
        return pd.DataFrame(daily_rows(analytics.daily_kpis(start, end)))

    if report_type == "top_products":
        limit = int(params.get("limit", 100))
        stats = analytics.item_stats(start, end, min_views=1)
        stats["category_id"] = pd.array([catalog.get(int(i)).category_id for i in stats["itemid"]], dtype="Int64")
        stats["available"] = [catalog.get(int(i)).available for i in stats["itemid"]]
        if params.get("category_id") is not None:
            stats = stats[stats["category_id"] == int(params["category_id"])]
        stats["conversion_rate"] = (stats["transactions"] / stats["views"]).round(4)
        stats["cart_rate"] = (stats["carts"] / stats["views"]).round(4)
        return stats.sort_values(["transactions", "carts", "views"], ascending=False).head(limit).reset_index(drop=True)

    if report_type == "cold_start_coverage":
        daily = analytics.daily_kpis(start, end)
        out = daily[["date", "visitors", "new_visitors"]].copy()
        out["date"] = out["date"].astype(str)
        out["returning_visitors"] = out["visitors"] - out["new_visitors"]
        out["cold_start_share"] = (out["new_visitors"] / out["visitors"]).round(4)
        return out

    raise ValueError(f"tipo de reporte desconocido: {report_type}")
