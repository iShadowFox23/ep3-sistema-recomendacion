# ML — modelo de recomendación

Filtrado colaborativo con **ALS para feedback implícito** (Hu et al., 2008; librería `implicit`),
entrenado sobre el dataset RetailRocket (vistas, carritos y compras).

## Flujo

```bash
pip install -r ml/requirements.txt
python ml/prepare_data.py                                   # data/raw -> data/processed (split temporal)
python ml/tune.py --out reports/tuning.csv                  # ajuste en valid (opcional)
python ml/train.py --splits train,valid --model-dir models/als
python ml/evaluate.py --model-dir models/als --split test --out reports/metrics_test.json
python -m pytest ml/tests
```

| Archivo | Rol |
|---|---|
| `prepare_data.py` | Pesos implícitos (view=1, addtocart=3, transaction=5), split temporal, categorías y disponibilidad |
| `recsys.py` | Matriz usuario-producto, entrenamiento ALS, artefacto (`model.npz`), inferencia numpy, métricas |
| `train.py` | Entrena y guarda el artefacto (local o Training Job de SageMaker: `SM_CHANNEL_TRAIN`, `SM_MODEL_DIR`) |
| `tune.py` | Grilla de hiperparámetros (train → valid) |
| `evaluate.py` | Métricas @10 vs baseline de popularidad (CP-13) |
| `inference/inference.py` | Handlers del endpoint de SageMaker (`model_fn`, `input_fn`, `predict_fn`, `output_fn`) |

## Datos y split

- Split temporal: train < 2015-08-21 · valid 2015-08-21..09-03 · test ≥ 2015-09-04.
- Al modelo factorizado entran usuarios con ≥ 2 productos distintos (260.421 usuarios, 225.307 productos,
  929.715 interacciones en train+valid). El resto se atiende por similitud de productos o popularidad.
- Confianza ALS = 1 + alpha · log(1 + peso).

## Hiperparámetros (ajustados en valid)

| factors | alpha | reg | NDCG@10 valid | Entrenamiento |
|---|---|---|---|---|
| 64 | 10 | 0,05 | 0,0184 | ≈27 s |
| 128 | 40 | 0,05 | 0,0260 | ≈29 s |
| **128** | **100** | **0,05** | **0,0273** | **≈28 s** |
| 256 | 100 | 0,5 | 0,0307 | ≈470 s |

Se eligió 128 factores: 256 mejora ~12% el NDCG pero entrena ~16 veces más lento y duplica el
artefacto. La regularización casi no influye. Detalle completo en `reports/tuning_grid*.csv`.

## Resultados en test (`reports/metrics_test.json`, 5.381 usuarios)

| Métrica | ALS | Baseline popularidad | Lift |
|---|---|---|---|
| precision@10 | 0,0081 | 0,0014 | ×5,7 |
| recall@10 | 0,0417 | 0,0050 | ×8,3 |
| F1@10 | 0,0136 | 0,0022 | ×6,2 |
| HitRate@10 | 6,5% | 1,2% | ×5,2 |
| NDCG@10 | 0,0268 | 0,0032 | ×8,4 |
| Cobertura de catálogo | 2,95% | 0,01% | — |
| accuracy@10 | 0,99994 | 0,99994 | — |

Los valores absolutos son bajos porque se mide descubrimiento (productos no vistos antes por el usuario)
en un dataset muy disperso; lo relevante es la mejora frente al baseline. **accuracy@10 es casi idéntica
en ambos modelos** (≈99,99%) porque casi todos los pares usuario-producto son negativos: no sirve para
comparar recomendadores y se reporta solo para justificarlo.

## Inferencia

- Estrategias: `als` (usuario conocido) · `item_similarity` (ficha/carrito o sesión) ·
  `popular_category` / `popular` (cold-start). Cada producto trae una explicación
  (`similar_to_history`: producto del historial más parecido y su similitud).
- Latencia local por petición (k=10, 300 usuarios): p50 5,6 ms · p95 6,9 ms · p99 7,3 ms. Carga del modelo ≈1,4 s.
- Artefacto ≈200 MB (`model.npz`). 87.696 productos (39%) no tienen señal colaborativa (solo los vieron
  usuarios fuera del modelo); para ellos se usa la popularidad de su categoría.
- Oportunidad de optimización: guardar factores en float16 o excluir productos sin señal para reducir el artefacto.
