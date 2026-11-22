"""Catálogo de productos para las reglas de negocio (stock y categoría).

En producción vendría de DynamoDB sincronizado con Shopify; en la demo se carga una vez desde
`catalog.csv` (local o S3), generado por `ml/export_serving_data.py`.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .storage import read_text


@dataclass(frozen=True)
class ItemInfo:
    category_id: int | None
    available: bool | None  # None = sin dato de stock


class Catalog:
    def __init__(self, items: dict[int, ItemInfo]):
        self._items = items

    def __len__(self) -> int:
        return len(self._items)

    @classmethod
    def from_csv_text(cls, text: str) -> "Catalog":
        items = {}
        for row in csv.DictReader(io.StringIO(text)):
            cat = int(row["category_id"])
            items[int(row["itemid"])] = ItemInfo(
                category_id=cat if cat >= 0 else None,
                available=row["available"] == "1",
            )
        return cls(items)

    @classmethod
    def load(cls, path: str | None) -> "Catalog":
        return cls.from_csv_text(read_text(path)) if path else cls({})

    def get(self, item_id: int) -> ItemInfo:
        return self._items.get(item_id, ItemInfo(category_id=None, available=None))
