"""Agregados analíticos diarios para business-api (backend `local`) y el dashboard.

Genera en `build/analytics/`:
- `kpis_daily.csv`: por día -> visitors, new_visitors, buyers, views, carts, transactions.
- `item_daily.csv`: por día y producto -> views, carts, transactions.

Son las mismas métricas que business-api calcula con Athena sobre el data lake (backend `athena`);
este archivo permite trabajar sin AWS y responder en milisegundos.

Uso:
    python ml/export_analytics.py [--raw data/raw/retailrocket] [--out build/analytics]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=Path("data/raw/retailrocket"))
    parser.add_argument("--out", type=Path, default=Path("build/analytics"))
    args = parser.parse_args()

    ev = pd.read_csv(args.raw / "events.csv", usecols=["timestamp", "visitorid", "event", "itemid"])
    ev["date"] = pd.to_datetime(ev["timestamp"], unit="ms").dt.strftime("%Y-%m-%d")
    for name, kind in (("views", "view"), ("carts", "addtocart"), ("transactions", "transaction")):
        ev[name] = (ev["event"] == kind).astype("int32")

    first_seen = ev.groupby("visitorid")["date"].min().rename("first_date")
    ev = ev.join(first_seen, on="visitorid")

    daily = ev.groupby("date").agg(
        visitors=("visitorid", "nunique"),
        views=("views", "sum"),
        carts=("carts", "sum"),
        transactions=("transactions", "sum"),
    )
    daily["new_visitors"] = ev[ev["date"] == ev["first_date"]].groupby("date")["visitorid"].nunique()
    daily["buyers"] = ev[ev["transactions"] == 1].groupby("date")["visitorid"].nunique()
    daily = daily.fillna(0).astype("int64").reset_index()

    item_daily = ev.groupby(["date", "itemid"])[["views", "carts", "transactions"]].sum().reset_index()

    args.out.mkdir(parents=True, exist_ok=True)
    daily.to_csv(args.out / "kpis_daily.csv", index=False)
    item_daily.to_csv(args.out / "item_daily.csv", index=False)
    print(f"kpis_daily.csv: {len(daily)} días ({daily['date'].min()} -> {daily['date'].max()})")
    print(f"item_daily.csv: {len(item_daily)} filas")


if __name__ == "__main__":
    main()
