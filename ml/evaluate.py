"""Evaluación offline del recomendador contra el baseline de popularidad (caso CP-13).

Protocolo:
- Usuarios evaluados: los que están en el modelo (historial en entrenamiento) y además
  interactúan en el split de evaluación.
- Relevantes: productos con los que el usuario interactúa en el split de evaluación y que
  NO había visto en entrenamiento (se mide descubrimiento, igual que en producción, donde
  se filtran los productos ya vistos).
- Ambos modelos recomiendan k=10 productos filtrando lo ya visto.

Uso:
    python ml/evaluate.py --model-dir models/als --split test --out reports/metrics_test.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recsys import Recommender, catalog_coverage, ranking_metrics  # noqa: E402

BATCH = 256


def relevant_sets(rec: Recommender, eval_interactions: pd.DataFrame) -> tuple[np.ndarray, list[set[int]]]:
    """Filas de usuario del modelo y su conjunto de productos relevantes (índices)."""
    item_index = {int(it): i for i, it in enumerate(rec.item_ids)}
    df = eval_interactions[eval_interactions["visitorid"].map(rec.is_known_user)]
    df = df[df["itemid"].isin(item_index)]
    rows, relevant = [], []
    for visitor, group in df.groupby("visitorid"):
        row = rec.user_row(visitor)
        seen = set(rec.history[row].indices.tolist())
        rel = {item_index[int(i)] for i in group["itemid"]} - seen
        if rel:
            rows.append(row)
            relevant.append(rel)
    return np.array(rows, dtype=np.int64), relevant


def evaluate(rec: Recommender, eval_interactions: pd.DataFrame, k: int = 10, max_users: int | None = None, seed: int = 0) -> dict:
    rows, relevant = relevant_sets(rec, eval_interactions)
    if max_users and len(rows) > max_users:
        pick = np.random.default_rng(seed).choice(len(rows), max_users, replace=False)
        rows, relevant = rows[pick], [relevant[i] for i in pick]

    als_recs = np.vstack([rec.recommend_batch(rows[i : i + BATCH], k) for i in range(0, len(rows), BATCH)])
    pop_recs = rec.popular_batch(rows, len(rows), k)
    n_items = len(rec.item_ids)

    als = ranking_metrics(als_recs, relevant, n_items, k)
    pop = ranking_metrics(pop_recs, relevant, n_items, k)
    als["catalog_coverage"] = catalog_coverage(als_recs, n_items)
    pop["catalog_coverage"] = catalog_coverage(pop_recs, n_items)
    lift = {m: (als[m] / pop[m] if pop[m] else None) for m in (f"precision@{k}", f"recall@{k}", f"ndcg@{k}")}
    return {"k": k, "als": als, "popularity_baseline": pop, "lift_vs_baseline": lift}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--model-dir", type=Path, default=Path("models/als"))
    parser.add_argument("--split", default="test")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    rec = Recommender.load(args.model_dir)
    interactions = pd.read_csv(args.processed / "interactions.csv")
    eval_df = interactions[interactions["split"] == args.split]
    result = evaluate(rec, eval_df, args.k)
    result["split"] = args.split
    result["model"] = rec.metadata
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
