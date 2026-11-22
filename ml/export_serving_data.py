"""Exporta los datos que necesitan los microservicios en línea (no el modelo).

Genera en `build/serving/`:
- `catalog.csv`: itemid, category_id, root_category_id, available — usado por rank-service
  para las reglas de negocio (stock y diversidad). En producción vendría de DynamoDB/Shopify.
- `popular.json`: populares globales y por categoría raíz — usado por el Fallback Handler de
  reco-service cuando rank-service o SageMaker no responden a tiempo.

Uso:
    python ml/export_serving_data.py [--model-dir models/als] [--out build/serving] [--upload]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recsys import Recommender  # noqa: E402

TOP_GLOBAL = 50
TOP_PER_CATEGORY = 20


def build_popular(rec: Recommender, catalog: pd.DataFrame) -> dict:
    available = catalog.set_index("itemid")["available"].reindex(rec.item_ids).fillna(0).to_numpy() == 1
    order = np.argsort(-rec.popularity, kind="stable")
    order = order[(rec.popularity[order] > 0) & available[order]]  # solo productos con stock
    by_category: dict[str, list[int]] = {}
    for idx in order:
        cat = int(rec.item_category[idx])
        if cat < 0:
            continue
        bucket = by_category.setdefault(str(cat), [])
        if len(bucket) < TOP_PER_CATEGORY:
            bucket.append(int(rec.item_ids[idx]))
    return {
        "global": [int(rec.item_ids[i]) for i in order[:TOP_GLOBAL]],
        "by_category": by_category,
        "source": rec.metadata.get("train_splits"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", type=Path, default=Path("models/als"))
    parser.add_argument("--processed", type=Path, default=Path("data/processed"))
    parser.add_argument("--out", type=Path, default=Path("build/serving"))
    parser.add_argument("--upload", action="store_true", help="sube los archivos al bucket del proyecto en S3")
    args = parser.parse_args()

    rec = Recommender.load(args.model_dir)
    items = pd.read_csv(args.processed / "items.csv")
    catalog = items[items["itemid"].isin(rec.item_ids)].copy()
    catalog["category_id"] = catalog["category_id"].fillna(-1).astype(int)
    catalog["root_category_id"] = catalog["root_category_id"].fillna(-1).astype(int)

    args.out.mkdir(parents=True, exist_ok=True)
    catalog.to_csv(args.out / "catalog.csv", index=False)
    popular = build_popular(rec, catalog)
    (args.out / "popular.json").write_text(json.dumps(popular), encoding="utf-8")
    print(f"catalog.csv: {len(catalog)} productos ({catalog['available'].mean():.1%} disponibles)")
    print(f"popular.json: {len(popular['global'])} globales, {len(popular['by_category'])} categorías")

    if args.upload:
        import boto3

        session = boto3.Session()
        account = session.client("sts").get_caller_identity()["Account"]
        bucket = f"reco-ep3-{account}-{session.region_name}"
        s3 = session.client("s3")
        for name in ("catalog.csv", "popular.json"):
            s3.upload_file(str(args.out / name), bucket, f"serving/{name}")
            print(f"subido: s3://{bucket}/serving/{name}")


if __name__ == "__main__":
    main()
