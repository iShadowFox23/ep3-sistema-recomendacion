# Datos

Dataset: **RetailRocket e-commerce dataset** (Kaggle) — https://www.kaggle.com/datasets/retailrocket/ecommerce-dataset

Reemplaza a los datos reales de Shopify del caso (no disponibles). Descargar con una cuenta de Kaggle y descomprimir en:

```
data/raw/retailrocket/
├── events.csv                  # view / addtocart / transaction (≈2,76 M eventos, 2015-05-03 → 2015-09-18)
├── item_properties_part1.csv   # propiedades de producto en el tiempo (categoryid, available, ...)
├── item_properties_part2.csv
└── category_tree.csv           # jerarquía de categorías
```

`data/raw/` y `data/processed/` están fuera de git. `data/processed/` lo genera `ml/prepare_data.py`.
