"""Entrena el recomendador ALS y guarda el artefacto servible.

Funciona igual en local y como script de un Training Job de SageMaker
(lee SM_CHANNEL_TRAIN y SM_MODEL_DIR si existen).

Uso local:
    python ml/train.py --splits train,valid --model-dir models/als
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recsys import (  # noqa: E402
    ALSParams,
    build_matrix,
    build_recommender,
    load_interactions,
    popularity_scores,
    train_als,
)

# Elegidos con ml/tune.py sobre el split de validación (reports/tuning_grid*.csv).
# 256 factores daba ~12% más NDCG@10 (0,0305 vs 0,0273), pero entrenaba ~16 veces más lento
# (≈470 s vs ≈28 s) y duplicaba el artefacto; para un endpoint ml.t3.medium/ml.m5.large y
# reentrenamiento semanal se prefirió 128 factores. La regularización casi no influyó.
DEFAULT_PARAMS = ALSParams(factors=128, regularization=0.05, alpha=100.0, iterations=15)


def train(processed: Path, splits: list[str], params: ALSParams, min_user_items: int = 2):
    interactions = load_interactions(processed, splits)
    data = build_matrix(interactions, min_user_items=min_user_items)
    start = time.time()
    user_factors, item_factors = train_als(data, params)
    elapsed = time.time() - start

    items_path = processed / "items.csv"
    items = pd.read_csv(items_path) if items_path.exists() else None
    metadata = {
        "model": "ALS (implicit feedback)",
        "params": params.to_dict(),
        "train_splits": splits,
        "min_user_items": min_user_items,
        "n_users": int(data.matrix.shape[0]),
        "n_items": int(data.matrix.shape[1]),
        "n_interactions": int(data.matrix.nnz),
        "train_seconds": round(elapsed, 1),
    }
    popularity = popularity_scores(interactions, data.item_ids)
    return build_recommender(data, user_factors, item_factors, popularity, items, metadata)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--processed", type=Path, default=Path(os.getenv("SM_CHANNEL_TRAIN", "data/processed")))
    parser.add_argument("--model-dir", type=Path, default=Path(os.getenv("SM_MODEL_DIR", "models/als")))
    parser.add_argument("--splits", default="train,valid", help="splits usados para entrenar")
    parser.add_argument("--factors", type=int, default=DEFAULT_PARAMS.factors)
    parser.add_argument("--regularization", type=float, default=DEFAULT_PARAMS.regularization)
    parser.add_argument("--alpha", type=float, default=DEFAULT_PARAMS.alpha)
    parser.add_argument("--iterations", type=int, default=DEFAULT_PARAMS.iterations)
    parser.add_argument("--min-user-items", type=int, default=2)
    args = parser.parse_args()

    params = ALSParams(args.factors, args.regularization, args.alpha, args.iterations)
    recommender = train(args.processed, args.splits.split(","), params, args.min_user_items)
    recommender.save(args.model_dir)
    print(json.dumps(recommender.metadata, indent=2))


if __name__ == "__main__":
    main()
