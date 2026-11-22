"""Núcleo del recomendador: matrices, entrenamiento ALS, artefacto y métricas.

El entrenamiento usa la librería `implicit` (ALS para feedback implícito, Hu et al., 2008).
La inferencia (`Recommender`) usa solo numpy, para que el endpoint de SageMaker no dependa
de `implicit` y para que la evaluación offline ejercite exactamente el mismo código que producción.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp

ARTIFACT_NPZ = "model.npz"
ARTIFACT_META = "metadata.json"


# --------------------------------------------------------------------------- datos


def load_interactions(processed: Path, splits: list[str]) -> pd.DataFrame:
    df = pd.read_csv(processed / "interactions.csv")
    df = df[df["split"].isin(splits)]
    # Si se combinan splits (p. ej. train+valid), se suman las interacciones del mismo par.
    return df.groupby(["visitorid", "itemid"], as_index=False).agg(
        weight=("weight", "sum"), last_ts=("last_ts", "max")
    )


@dataclass
class InteractionMatrix:
    matrix: sp.csr_matrix  # usuarios x productos, valores = peso implícito
    user_ids: np.ndarray  # índice -> visitorid
    item_ids: np.ndarray  # índice -> itemid

    @property
    def user_index(self) -> dict[int, int]:
        return {int(u): i for i, u in enumerate(self.user_ids)}

    @property
    def item_index(self) -> dict[int, int]:
        return {int(it): i for i, it in enumerate(self.item_ids)}


def build_matrix(interactions: pd.DataFrame, min_user_items: int = 2) -> InteractionMatrix:
    """Construye la matriz usuario-producto.

    Solo entran al modelo factorizado los usuarios con al menos `min_user_items` productos
    distintos: con un solo producto no hay señal colaborativa y su vector solo agrega tamaño
    al artefacto. Esos usuarios se atienden por similitud de productos o popularidad.
    Todos los productos con interacciones se conservan.
    """
    item_ids = np.sort(interactions["itemid"].unique())
    per_user = interactions.groupby("visitorid")["itemid"].transform("size")
    kept = interactions[per_user >= min_user_items]
    user_ids = np.sort(kept["visitorid"].unique())

    rows = np.searchsorted(user_ids, kept["visitorid"].to_numpy())
    cols = np.searchsorted(item_ids, kept["itemid"].to_numpy())
    matrix = sp.csr_matrix(
        (kept["weight"].to_numpy(dtype=np.float32), (rows, cols)),
        shape=(len(user_ids), len(item_ids)),
    )
    return InteractionMatrix(matrix=matrix, user_ids=user_ids, item_ids=item_ids)


def popularity_scores(interactions: pd.DataFrame, item_ids: np.ndarray, recent_days: int = 14) -> np.ndarray:
    """Peso total de cada producto en los últimos `recent_days` días del periodo de entrenamiento."""
    cutoff = interactions["last_ts"].max() - recent_days * 24 * 3600 * 1000
    recent = interactions[interactions["last_ts"] >= cutoff]
    totals = recent.groupby("itemid")["weight"].sum()
    return totals.reindex(item_ids, fill_value=0.0).to_numpy(dtype=np.float32)


# --------------------------------------------------------------------------- entrenamiento


@dataclass
class ALSParams:
    factors: int = 64
    regularization: float = 0.05
    alpha: float = 10.0
    iterations: int = 15
    random_state: int = 42

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def train_als(data: InteractionMatrix, params: ALSParams) -> tuple[np.ndarray, np.ndarray]:
    """Entrena ALS y devuelve (factores de usuario, factores de producto) en float32."""
    from implicit.als import AlternatingLeastSquares  # solo se necesita para entrenar

    model = AlternatingLeastSquares(
        factors=params.factors,
        regularization=params.regularization,
        iterations=params.iterations,
        random_state=params.random_state,
        calculate_training_loss=False,
    )
    # Confianza = 1 + alpha * log(1 + peso): suaviza usuarios con cientos de vistas al mismo producto.
    confidence = data.matrix.copy()
    confidence.data = (1.0 + params.alpha * np.log1p(confidence.data)).astype(np.float32)
    model.fit(confidence, show_progress=False)
    return np.asarray(model.user_factors, dtype=np.float32), np.asarray(model.item_factors, dtype=np.float32)


# --------------------------------------------------------------------------- artefacto + inferencia


@dataclass
class Recommender:
    """Modelo listo para servir: factores, historial de usuarios, popularidad y metadatos."""

    user_factors: np.ndarray
    item_factors: np.ndarray
    user_ids: np.ndarray
    item_ids: np.ndarray
    history: sp.csr_matrix  # usuarios x productos (para filtrar vistos y explicar)
    popularity: np.ndarray
    item_category: np.ndarray  # categoría raíz por producto (-1 si se desconoce)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._user_index = {int(u): i for i, u in enumerate(self.user_ids)}
        self._item_index = {int(it): i for i, it in enumerate(self.item_ids)}
        norms = np.linalg.norm(self.item_factors, axis=1, keepdims=True)
        self._item_unit = self.item_factors / np.maximum(norms, 1e-8)
        # Productos que solo vieron usuarios fuera del modelo quedan con vector ~0: sin señal colaborativa.
        self._item_has_signal = norms[:, 0] > 1e-6
        self._popular_order = np.argsort(-self.popularity, kind="stable")

    # ---- persistencia
    def save(self, model_dir: Path) -> None:
        model_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            model_dir / ARTIFACT_NPZ,
            user_factors=self.user_factors,
            item_factors=self.item_factors,
            user_ids=self.user_ids,
            item_ids=self.item_ids,
            history_indptr=self.history.indptr,
            history_indices=self.history.indices,
            popularity=self.popularity,
            item_category=self.item_category,
        )
        (model_dir / ARTIFACT_META).write_text(json.dumps(self.metadata, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, model_dir: Path) -> "Recommender":
        z = np.load(model_dir / ARTIFACT_NPZ)
        n_users, n_items = len(z["user_ids"]), len(z["item_ids"])
        indices = z["history_indices"]
        history = sp.csr_matrix(
            (np.ones(len(indices), dtype=np.float32), indices, z["history_indptr"]), shape=(n_users, n_items)
        )
        meta_path = model_dir / ARTIFACT_META
        metadata = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        return cls(
            user_factors=z["user_factors"],
            item_factors=z["item_factors"],
            user_ids=z["user_ids"],
            item_ids=z["item_ids"],
            history=history,
            popularity=z["popularity"],
            item_category=z["item_category"],
            metadata=metadata,
        )

    # ---- consultas
    def is_known_user(self, visitor_id: int) -> bool:
        return int(visitor_id) in self._user_index

    def user_row(self, visitor_id: int) -> int | None:
        return self._user_index.get(int(visitor_id))

    def item_rows(self, item_ids) -> np.ndarray:
        return np.array([self._item_index[int(i)] for i in item_ids if int(i) in self._item_index], dtype=np.int64)

    def with_signal(self, item_rows: np.ndarray) -> np.ndarray:
        return item_rows[self._item_has_signal[item_rows]]

    def recommend_batch(self, user_rows: np.ndarray, k: int = 10, filter_seen: bool = True) -> np.ndarray:
        """Top-k (índices de producto) para varios usuarios conocidos del modelo."""
        scores = self.user_factors[user_rows] @ self.item_factors.T
        if filter_seen:
            sub = self.history[user_rows]
            scores[sub.nonzero()] = -np.inf
        return _top_k(scores, k)

    def popular_batch(self, user_rows: np.ndarray | None, n_users: int, k: int = 10, filter_seen: bool = True) -> np.ndarray:
        """Baseline de popularidad (filtrando lo ya visto si el usuario es conocido)."""
        pool = self._popular_order[: k + 500]
        out = np.empty((n_users, k), dtype=np.int64)
        for row in range(n_users):
            seen = set()
            if filter_seen and user_rows is not None:
                seen = set(self.history[user_rows[row]].indices.tolist())
            picked = [i for i in pool if i not in seen][:k]
            out[row] = picked
        return out

    def similar_to_items(self, item_rows: np.ndarray, k: int = 10) -> np.ndarray:
        """Productos más parecidos (coseno en el espacio latente) a un conjunto de productos."""
        query = self._item_unit[item_rows].mean(axis=0)
        scores = self._item_unit @ query
        scores[item_rows] = -np.inf
        return _top_k(scores[None, :], k)[0]

    def popular_in_category(self, category: int | None, k: int = 10, exclude: set[int] | None = None) -> np.ndarray:
        exclude = exclude or set()
        order = self._popular_order
        if category is not None:
            order = order[self.item_category[order] == category]
        return np.array([i for i in order[: k + len(exclude)] if i not in exclude][:k], dtype=np.int64)

    def explain(self, user_history_rows: np.ndarray, item_row: int) -> tuple[int | None, float]:
        """Producto del historial más parecido al recomendado: base de la explicación
        "recomendado porque interactuaste con X"."""
        if len(user_history_rows) == 0:
            return None, 0.0
        sims = self._item_unit[user_history_rows] @ self._item_unit[item_row]
        best = int(np.argmax(sims))
        return int(self.item_ids[user_history_rows[best]]), float(sims[best])


def _top_k(scores: np.ndarray, k: int) -> np.ndarray:
    k = min(k, scores.shape[1])
    part = np.argpartition(-scores, k - 1, axis=1)[:, :k]
    order = np.argsort(-np.take_along_axis(scores, part, axis=1), axis=1, kind="stable")
    return np.take_along_axis(part, order, axis=1)


def build_recommender(
    data: InteractionMatrix,
    user_factors: np.ndarray,
    item_factors: np.ndarray,
    popularity: np.ndarray,
    items: pd.DataFrame | None,
    metadata: dict,
) -> Recommender:
    history = data.matrix.copy()
    history.data[:] = 1.0
    if items is not None:
        cat = items.set_index("itemid")["root_category_id"].reindex(data.item_ids)
        item_category = cat.fillna(-1).to_numpy(dtype=np.int64)
    else:
        item_category = np.full(len(data.item_ids), -1, dtype=np.int64)
    return Recommender(
        user_factors=user_factors,
        item_factors=item_factors,
        user_ids=data.user_ids,
        item_ids=data.item_ids,
        history=history.tocsr(),
        popularity=popularity,
        item_category=item_category,
        metadata=metadata,
    )


# --------------------------------------------------------------------------- métricas


def ranking_metrics(recommended: np.ndarray, relevant: list[set[int]], n_items: int, k: int = 10) -> dict:
    """Métricas top-k promediadas por usuario.

    - precision@k = aciertos / k
    - recall@k    = aciertos / |relevantes|
    - F1@k        = media armónica de precision@k y recall@k promedios
    - HitRate@k   = % de usuarios con al menos un acierto
    - NDCG@k      = ganancia acumulada descontada normalizada (premia aciertos arriba en la lista)
    - accuracy@k  = (VP + VN) / n_items, tratando "recomendado" como predicción positiva. Se
                    reporta solo para mostrar que es engañosa con datos tan desbalanceados.
    """
    discounts = 1.0 / np.log2(np.arange(2, k + 2))
    precision, recall, hits, ndcg, accuracy = [], [], [], [], []
    for recs, rel in zip(recommended, relevant):
        if not rel:
            continue
        flags = np.array([int(r) in rel for r in recs[:k]], dtype=float)
        tp = flags.sum()
        precision.append(tp / k)
        recall.append(tp / len(rel))
        hits.append(float(tp > 0))
        ideal = discounts[: min(len(rel), k)].sum()
        ndcg.append(float((flags * discounts[: len(flags)]).sum() / ideal))
        fp, fn = k - tp, len(rel) - tp
        accuracy.append((n_items - fp - fn) / n_items)
    p, r = float(np.mean(precision)), float(np.mean(recall))
    return {
        f"precision@{k}": p,
        f"recall@{k}": r,
        f"f1@{k}": 2 * p * r / (p + r) if p + r > 0 else 0.0,
        f"hitrate@{k}": float(np.mean(hits)),
        f"ndcg@{k}": float(np.mean(ndcg)),
        f"accuracy@{k}": float(np.mean(accuracy)),
        "users_evaluated": len(precision),
    }


def catalog_coverage(recommended: np.ndarray, n_items: int) -> float:
    return float(len(np.unique(recommended)) / n_items)
