"""Construcción de dependencias a partir de variables de entorno (compartida por la API y el worker).

    ANALYTICS_BACKEND   local | athena                     (por defecto: local)
    ANALYTICS_DIR       carpeta con kpis_daily.csv e item_daily.csv (backend local)
    ATHENA_DATABASE     (por defecto: reco_ep3)
    ATHENA_OUTPUT       s3://<bucket>/athena-results/      (backend athena)
    CATALOG_PATH        ruta local o s3:// a catalog.csv
    JOBS_TABLE          tabla DynamoDB de trabajos; vacío = archivos en JOBS_DIR
    REPORTS_BUCKET      bucket S3 de reportes; vacío = archivos en REPORTS_DIR
    RABBITMQ_URL        amqp://usuario:clave@host:5672/
    RECO_SERVICE_URL    URL interna de reco-service
    RECO_API_KEY        API key que business-api usa al llamar a reco-service
    ALLOW_FAULT_INJECTION  true solo en pruebas/demo (permite params.force_error)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .analytics import Analytics, build_analytics
from .catalog import Catalog
from .jobs import JobStore, build_job_store
from .storage import ReportStore, build_report_store


@dataclass
class Components:
    analytics: Analytics
    catalog: Catalog
    jobs: JobStore
    reports: ReportStore


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def build_components() -> Components:
    return Components(
        analytics=build_analytics(
            env("ANALYTICS_BACKEND", "local"),
            env("ANALYTICS_DIR", "build/analytics"),
            env("ATHENA_DATABASE", "reco_ep3"),
            env("ATHENA_OUTPUT"),
        ),
        catalog=Catalog.load(env("CATALOG_PATH")),
        jobs=build_job_store(env("JOBS_TABLE"), env("JOBS_DIR", "build/shared/jobs")),
        reports=build_report_store(env("REPORTS_BUCKET"), env("REPORTS_DIR", "build/shared/reports")),
    )


def fault_injection_allowed() -> bool:
    return env("ALLOW_FAULT_INJECTION", "false").lower() == "true"
