"""Búsqueda en grilla de hiperparámetros de ALS: entrena con `train` y evalúa en `valid`.

El split `test` no se toca aquí, para que la métrica final no quede sesgada por el ajuste.

Uso:
    python ml/tune.py --factors 32,64,128 --alpha 2,10,40 --regularization 0.05 --out reports/tuning.csv
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate import evaluate  # noqa: E402
from recsys import ALSParams, build_matrix, build_recommender, load_interactions, popularity_scores, train_als  # noqa: E402


def floats(text: str) -> list[float]:
    return [float(x) for x in text.split(",")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--factors", default="32,64,128")
    parser.add_argument("--alpha", default="2,10,40")
    parser.add_argument("--regularization", default="0.05")
    parser.add_argument("--iterations", default="15")
    parser.add_argument("--out", type=Path, default=Path("reports/tuning.csv"))
    args = parser.parse_args()

    train_df = load_interactions(args.processed, ["train"])
    data = build_matrix(train_df)
    popularity = popularity_scores(train_df, data.item_ids)
    all_df = pd.read_csv(args.processed / "interactions.csv")
    valid_df = all_df[all_df["split"] == "valid"]

    rows = []
    grid = itertools.product(
        [int(f) for f in args.factors.split(",")],
        floats(args.alpha),
        floats(args.regularization),
        [int(i) for i in args.iterations.split(",")],
    )
    for factors, alpha, reg, iters in grid:
        params = ALSParams(factors=factors, regularization=reg, alpha=alpha, iterations=iters)
        start = time.time()
        uf, itf = train_als(data, params)
        rec = build_recommender(data, uf, itf, popularity, None, {})
        result = evaluate(rec, valid_df)["als"]
        row = {**params.to_dict(), **result, "seconds": round(time.time() - start, 1)}
        rows.append(row)
        print({k: (round(v, 5) if isinstance(v, float) else v) for k, v in row.items()}, flush=True)

    out = pd.DataFrame(rows).sort_values("ndcg@10", ascending=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.out, index=False)
    print("\nMejor configuración (por NDCG@10 en valid):")
    print(out.iloc[0].to_string())


if __name__ == "__main__":
    main()
