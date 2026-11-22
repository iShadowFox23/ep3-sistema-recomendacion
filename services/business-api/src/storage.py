"""Archivos: lectura local/S3 y almacenamiento de reportes (S3 con URL prefirmada, o carpeta local)."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


def read_text(path: str) -> str:
    if path.startswith("s3://"):
        import boto3

        bucket, _, key = path[len("s3://") :].partition("/")
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    return Path(path).read_text(encoding="utf-8")


class ReportStore(Protocol):
    def save(self, job_id: str, content: str) -> str: ...
    def link(self, job_id: str, location: str) -> str: ...


class LocalReportStore:
    """Desarrollo local: CSV en una carpeta compartida; se descarga por la propia API."""

    def __init__(self, directory: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, job_id: str) -> Path:
        return self.directory / f"{job_id}.csv"

    def save(self, job_id: str, content: str) -> str:
        self.path(job_id).write_text(content, encoding="utf-8")
        return str(self.path(job_id))

    def link(self, job_id: str, location: str) -> str:
        return f"/v1/reports/{job_id}/download"


class S3ReportStore:
    """AWS: CSV en S3 (bucket privado y cifrado); el cliente recibe una URL prefirmada de 15 min."""

    def __init__(self, bucket: str, prefix: str = "reports", expires_s: int = 900):
        import boto3
        from botocore.config import Config

        self.bucket, self.prefix, self.expires_s = bucket, prefix, expires_s
        self._s3 = boto3.client("s3", config=Config(signature_version="s3v4"))  # firma SigV4

    def save(self, job_id: str, content: str) -> str:
        key = f"{self.prefix}/{job_id}.csv"
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=content.encode("utf-8"), ContentType="text/csv")
        return f"s3://{self.bucket}/{key}"

    def link(self, job_id: str, location: str) -> str:
        key = location.split(f"s3://{self.bucket}/", 1)[1]
        return self._s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expires_s
        )


def build_report_store(bucket: str | None, local_dir: str) -> ReportStore:
    return S3ReportStore(bucket) if bucket else LocalReportStore(local_dir)
