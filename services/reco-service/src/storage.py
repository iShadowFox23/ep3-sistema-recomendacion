"""Lectura de archivos desde disco local o S3 (s3://bucket/clave)."""
from __future__ import annotations

from pathlib import Path


def read_text(path: str) -> str:
    if path.startswith("s3://"):
        import boto3

        bucket, _, key = path[len("s3://") :].partition("/")
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        return body.decode("utf-8")
    return Path(path).read_text(encoding="utf-8")
