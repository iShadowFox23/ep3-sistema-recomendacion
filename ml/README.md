# ML

1. `prepare_data.py` — lee `data/raw/retailrocket/`, limpia y genera `data/processed/` (split temporal).
2. `train.py` — entrena el modelo (filtrado colaborativo implícito, ALS) en SageMaker (ml.m5.xlarge / ml.c5.xlarge).
3. `evaluate.py` — precision@10, recall@10, F1@10, HitRate@10, NDCG@10 vs baseline de popularidad.
4. `inference/inference.py` — handlers del endpoint real-time de SageMaker.
