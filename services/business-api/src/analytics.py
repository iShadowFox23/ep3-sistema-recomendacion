"""Fuente analítica: agregados locales (CSV) o Athena sobre el data lake en S3.

Ambos backends exponen lo mismo:
- `date_range()`            -> (primer día, último día) con datos
- `daily_kpis(start, end)`  -> filas por día: visitors, new_visitors, buyers, views, carts, transactions
- `item_stats(start, end)`  -> filas por producto: itemid, views, carts, transactions
"""
from __future__ import annotations

import io
import time
from datetime import date
from typing import Protocol

import pandas as pd

KPI_COLUMNS = ["date", "visitors", "new_visitors", "buyers", "views", "carts", "transactions"]
ITEM_COLUMNS = ["itemid", "views", "carts", "transactions"]


class Analytics(Protocol):
    def date_range(self) -> tuple[date, date]: ...
    def daily_kpis(self, start: date, end: date) -> pd.DataFrame: ...
    def item_stats(self, start: date, end: date, min_views: int = 1) -> pd.DataFrame: ...


class LocalAnalytics:
    """Lee `kpis_daily.csv` e `item_daily.csv` (generados por ml/export_analytics.py)."""

    def __init__(self, kpis: pd.DataFrame, item_daily: pd.DataFrame):
        self._kpis = kpis.assign(date=pd.to_datetime(kpis["date"]).dt.date)
        self._items = item_daily.assign(date=pd.to_datetime(item_daily["date"]).dt.date)

    @classmethod
    def from_dir(cls, directory: str) -> "LocalAnalytics":
        return cls(pd.read_csv(f"{directory}/kpis_daily.csv"), pd.read_csv(f"{directory}/item_daily.csv"))

    def date_range(self) -> tuple[date, date]:
        return self._kpis["date"].min(), self._kpis["date"].max()

    def daily_kpis(self, start: date, end: date) -> pd.DataFrame:
        df = self._kpis[(self._kpis["date"] >= start) & (self._kpis["date"] <= end)]
        return df[KPI_COLUMNS].reset_index(drop=True)

    def item_stats(self, start: date, end: date, min_views: int = 1) -> pd.DataFrame:
        df = self._items[(self._items["date"] >= start) & (self._items["date"] <= end)]
        agg = df.groupby("itemid")[["views", "carts", "transactions"]].sum().reset_index()
        return agg[agg["views"] >= min_views][ITEM_COLUMNS].reset_index(drop=True)


class AthenaAnalytics:
    """Consulta las tablas `events` del data lake (infra/scripts/datalake.py). Las fechas llegan
    validadas como `date`, por lo que interpolarlas en el SQL no permite inyección."""

    def __init__(self, database: str, output_location: str, poll_s: float = 0.4):
        import boto3

        self.database, self.output, self.poll_s = database, output_location, poll_s
        self._athena = boto3.client("athena")
        self._s3 = boto3.client("s3")
        self._range: tuple[date, date] | None = None

    def _query(self, sql: str) -> pd.DataFrame:
        qid = self._athena.start_query_execution(
            QueryString=sql,
            QueryExecutionContext={"Database": self.database},
            ResultConfiguration={"OutputLocation": self.output},
        )["QueryExecutionId"]
        while True:
            execution = self._athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
            state = execution["Status"]["State"]
            if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                break
            time.sleep(self.poll_s)
        if state != "SUCCEEDED":
            raise RuntimeError(f"Athena {state}: {execution['Status'].get('StateChangeReason')}")
        # Leer el CSV de resultados directo desde S3 es mucho más rápido que paginar get_query_results.
        bucket, _, key = execution["ResultConfiguration"]["OutputLocation"][len("s3://") :].partition("/")
        body = self._s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return pd.read_csv(io.BytesIO(body))

    def date_range(self) -> tuple[date, date]:
        if self._range is None:
            df = self._query('SELECT min(date(from_unixtime("timestamp" / 1000))) AS lo, max(date(from_unixtime("timestamp" / 1000))) AS hi FROM events')
            self._range = (pd.to_datetime(df["lo"][0]).date(), pd.to_datetime(df["hi"][0]).date())
        return self._range

    def daily_kpis(self, start: date, end: date) -> pd.DataFrame:
        sql = f"""
        WITH e AS (SELECT visitorid, event, date(from_unixtime("timestamp" / 1000)) AS d FROM events),
             f AS (SELECT visitorid, min(d) AS first_d FROM e GROUP BY visitorid)
        SELECT cast(e.d AS varchar) AS date,
               count(DISTINCT e.visitorid) AS visitors,
               count(DISTINCT CASE WHEN e.d = f.first_d THEN e.visitorid END) AS new_visitors,
               count(DISTINCT CASE WHEN e.event = 'transaction' THEN e.visitorid END) AS buyers,
               sum(CASE WHEN e.event = 'view' THEN 1 ELSE 0 END) AS views,
               sum(CASE WHEN e.event = 'addtocart' THEN 1 ELSE 0 END) AS carts,
               sum(CASE WHEN e.event = 'transaction' THEN 1 ELSE 0 END) AS transactions
        FROM e JOIN f ON e.visitorid = f.visitorid
        WHERE e.d BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
        GROUP BY e.d ORDER BY e.d"""
        df = self._query(sql)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df[KPI_COLUMNS]

    def item_stats(self, start: date, end: date, min_views: int = 1) -> pd.DataFrame:
        sql = f"""
        SELECT itemid,
               sum(CASE WHEN event = 'view' THEN 1 ELSE 0 END) AS views,
               sum(CASE WHEN event = 'addtocart' THEN 1 ELSE 0 END) AS carts,
               sum(CASE WHEN event = 'transaction' THEN 1 ELSE 0 END) AS transactions
        FROM events
        WHERE date(from_unixtime("timestamp" / 1000)) BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
        GROUP BY itemid
        HAVING sum(CASE WHEN event = 'view' THEN 1 ELSE 0 END) >= {int(min_views)}"""
        return self._query(sql)[ITEM_COLUMNS]


def build_analytics(backend: str, local_dir: str, database: str, output_location: str | None) -> Analytics:
    if backend == "athena":
        if not output_location:
            raise ValueError("ATHENA_OUTPUT es obligatorio con ANALYTICS_BACKEND=athena")
        return AthenaAnalytics(database, output_location)
    return LocalAnalytics.from_dir(local_dir)
