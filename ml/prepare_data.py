"""Preparación del dataset RetailRocket para el sistema de recomendación.

Lee `data/raw/retailrocket/` y genera en `data/processed/`:

- `interactions.csv`: interacciones agregadas por (split, visitorid, itemid) con su peso implícito.
- `items.csv`: categoría (hoja y raíz) y disponibilidad (último valor conocido) de cada producto.
- `split_info.json`: fechas de corte y conteos por split.

Split temporal (sin mirar el futuro):
    train  < 2015-08-21
    valid  2015-08-21 .. 2015-09-03   (ajuste de hiperparámetros)
    test   >= 2015-09-04              (evaluación final, últimas 2 semanas)

Uso:
    python ml/prepare_data.py [--raw data/raw/retailrocket] [--out data/processed]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

# Peso implícito de cada evento: una compra dice más que una vista.
EVENT_WEIGHTS = {"view": 1.0, "addtocart": 3.0, "transaction": 5.0}

VALID_START = pd.Timestamp("2015-08-21")
TEST_START = pd.Timestamp("2015-09-04")


def assign_split(ts: pd.Series) -> pd.Series:
    split = pd.Series("train", index=ts.index)
    split[ts >= VALID_START] = "valid"
    split[ts >= TEST_START] = "test"
    return split


def load_events(raw: Path) -> pd.DataFrame:
    events = pd.read_csv(
        raw / "events.csv",
        usecols=["timestamp", "visitorid", "event", "itemid"],
        dtype={"visitorid": "int64", "itemid": "int64", "event": "category"},
    )
    events = events[events["event"].isin(list(EVENT_WEIGHTS))]
    events["ts"] = pd.to_datetime(events["timestamp"], unit="ms")
    events["weight"] = events["event"].map(EVENT_WEIGHTS).astype("float32")
    events["split"] = assign_split(events["ts"])
    return events


def aggregate_interactions(events: pd.DataFrame) -> pd.DataFrame:
    """Suma pesos y cuenta eventos por (split, usuario, producto)."""
    keys = ["split", "visitorid", "itemid"]
    frame = events[keys + ["weight", "timestamp"]].copy()
    for event in EVENT_WEIGHTS:
        frame[f"n_{event}"] = (events["event"] == event).astype("int32")
    agg = frame.groupby(keys, sort=False).agg(
        n_view=("n_view", "sum"),
        n_addtocart=("n_addtocart", "sum"),
        n_transaction=("n_transaction", "sum"),
        weight=("weight", "sum"),
        last_ts=("timestamp", "max"),
    )
    return agg.reset_index()


def root_category(tree: pd.DataFrame) -> dict[int, int]:
    parent = dict(zip(tree["categoryid"], tree["parentid"]))
    roots: dict[int, int] = {}
    for cat in parent:
        node, seen = cat, set()
        while pd.notna(parent.get(node)) and node not in seen:
            seen.add(node)
            node = int(parent[node])
        roots[cat] = node
    return roots


def load_items(raw: Path) -> pd.DataFrame:
    """Último valor conocido de categoryid y available por producto."""
    parts = []
    for name in ("item_properties_part1.csv", "item_properties_part2.csv"):
        for chunk in pd.read_csv(raw / name, chunksize=2_000_000, dtype={"value": str}):
            parts.append(chunk[chunk["property"].isin(["categoryid", "available"])])
    props = pd.concat(parts)
    latest = (
        props.sort_values("timestamp")
        .groupby(["itemid", "property"])["value"]
        .last()
        .unstack()
        .rename(columns={"categoryid": "category_id"})
    )
    items = latest.reset_index()
    items["category_id"] = pd.to_numeric(items["category_id"], errors="coerce").astype("Int64")
    items["available"] = pd.to_numeric(items["available"], errors="coerce").fillna(0).astype("int8")

    tree = pd.read_csv(raw / "category_tree.csv")
    roots = root_category(tree)
    items["root_category_id"] = items["category_id"].map(roots).astype("Int64")
    return items[["itemid", "category_id", "root_category_id", "available"]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=Path("data/raw/retailrocket"))
    parser.add_argument("--out", type=Path, default=Path("data/processed"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    events = load_events(args.raw)
    interactions = aggregate_interactions(events)
    interactions.to_csv(args.out / "interactions.csv", index=False)

    items = load_items(args.raw)
    items.to_csv(args.out / "items.csv", index=False)

    info = {
        "event_weights": EVENT_WEIGHTS,
        "valid_start": str(VALID_START.date()),
        "test_start": str(TEST_START.date()),
        "date_min": str(events["ts"].min().date()),
        "date_max": str(events["ts"].max().date()),
        "events_per_split": events["split"].value_counts().to_dict(),
        "events_per_type": events["event"].value_counts().astype(int).to_dict(),
        "interactions_per_split": interactions["split"].value_counts().to_dict(),
        "visitors": int(events["visitorid"].nunique()),
        "items_with_events": int(events["itemid"].nunique()),
        "items_with_properties": int(len(items)),
    }
    (args.out / "split_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
