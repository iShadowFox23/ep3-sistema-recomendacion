"""Estado de los trabajos de reportes: DynamoDB (AWS) o archivos JSON en una carpeta compartida (local)."""
from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Protocol

STATUSES = ("queued", "running", "retrying", "done", "failed")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class JobStore(Protocol):
    def create(self, job: dict) -> None: ...
    def get(self, job_id: str) -> dict | None: ...
    def update(self, job_id: str, **fields) -> None: ...


class FileJobStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def create(self, job: dict) -> None:
        self._path(job["job_id"]).write_text(json.dumps(job), encoding="utf-8")

    def get(self, job_id: str) -> dict | None:
        path = self._path(job_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def update(self, job_id: str, **fields) -> None:
        job = self.get(job_id) or {"job_id": job_id}
        job.update(fields, updated_at=now())
        tmp = self._path(job_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(job), encoding="utf-8")
        tmp.replace(self._path(job_id))


class DynamoJobStore:
    """Tabla `report_jobs` (on-demand, clave `job_id`, TTL de 7 días en `expires_at`)."""

    def __init__(self, table_name: str, ttl_days: int = 7):
        import boto3

        self.table_name, self.ttl_s = table_name, ttl_days * 86400
        self._client = boto3.client("dynamodb")
        self._table = boto3.resource("dynamodb").Table(table_name)

    def ensure_table(self) -> None:
        try:
            self._client.describe_table(TableName=self.table_name)
            return
        except self._client.exceptions.ResourceNotFoundException:
            pass
        self._client.create_table(
            TableName=self.table_name,
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
            SSESpecification={"Enabled": True},
        )
        self._client.get_waiter("table_exists").wait(TableName=self.table_name)
        self._client.update_time_to_live(
            TableName=self.table_name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "expires_at"}
        )

    def create(self, job: dict) -> None:
        item = {**job, "expires_at": int(time.time()) + self.ttl_s, "params": json.dumps(job.get("params", {}))}
        self._table.put_item(Item=item)

    def get(self, job_id: str) -> dict | None:
        item = self._table.get_item(Key={"job_id": job_id}).get("Item")
        if not item:
            return None
        item.pop("expires_at", None)
        item["params"] = json.loads(item.get("params") or "{}")
        return {k: (int(v) if isinstance(v, Decimal) else v) for k, v in item.items()}  # DynamoDB devuelve Decimal

    def update(self, job_id: str, **fields) -> None:
        fields["updated_at"] = now()
        names = {f"#{k}": k for k in fields}
        values = {f":{k}": v for k, v in fields.items()}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
        self._table.update_item(
            Key={"job_id": job_id}, UpdateExpression=expr, ExpressionAttributeNames=names, ExpressionAttributeValues=values
        )


def build_job_store(table: str | None, local_dir: str) -> JobStore:
    if table:
        store = DynamoJobStore(table)
        store.ensure_table()
        return store
    return FileJobStore(local_dir)
