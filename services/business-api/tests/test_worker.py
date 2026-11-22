"""Pruebas de report-worker: generación (CP-11), reintentos y dead-letter (CP-12), duplicados."""
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from src import worker
from src.broker import MAX_ATTEMPTS


class FakeChannel:
    def __init__(self):
        self.acks, self.nacks, self.published = [], [], []

    def basic_ack(self, delivery_tag):
        self.acks.append(delivery_tag)

    def basic_nack(self, delivery_tag, requeue):
        self.nacks.append((delivery_tag, requeue))

    def basic_publish(self, exchange, routing_key, body, properties, mandatory):
        self.published.append((json.loads(body), properties.headers["x-attempt"]))


def deliver(components, message, attempt=1):
    channel = FakeChannel()
    method = SimpleNamespace(delivery_tag=1)
    props = SimpleNamespace(headers={"x-attempt": attempt})
    worker.handle(components, channel, method, props, json.dumps(message).encode())
    return channel


def new_job(components, report_type="kpi_summary", **params):
    message = {
        "job_id": f"job-{report_type}",
        "report_type": report_type,
        "params": {"from": "2015-09-01", "to": "2015-09-05", **params},
    }
    components.jobs.create({**message, "status": "queued", "attempts": 0, "created_at": "t"})
    return message


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(worker.time, "sleep", lambda s: None)


@pytest.mark.parametrize("report_type,rows", [("kpi_summary", 5), ("top_products", 5), ("cold_start_coverage", 5)])
def test_cp11_generates_and_stores_report(components, report_type, rows):
    message = new_job(components, report_type)
    channel = deliver(components, message)
    job = components.jobs.get(message["job_id"])
    assert channel.acks == [1] and job["status"] == "done" and job["rows"] == rows
    frame = pd.read_csv(components.reports.path(message["job_id"]))
    assert len(frame) == rows


def test_top_products_respects_category_and_limit(components):
    message = new_job(components, "top_products", category_id=10, limit=2)
    deliver(components, message)
    frame = pd.read_csv(components.reports.path(message["job_id"]))
    assert list(frame["itemid"]) == [1, 4]  # ordenado por compras; solo categoría 10


def test_failure_is_retried_with_incremented_attempt(components, monkeypatch):
    monkeypatch.setenv("ALLOW_FAULT_INJECTION", "true")
    message = new_job(components, force_error=True)
    channel = deliver(components, message, attempt=1)
    assert channel.acks == [1] and channel.nacks == []
    assert channel.published == [(message, 2)]
    assert components.jobs.get(message["job_id"])["status"] == "retrying"


def test_cp12_last_failure_goes_to_dead_letter_queue(components, monkeypatch):
    monkeypatch.setenv("ALLOW_FAULT_INJECTION", "true")
    message = new_job(components, force_error=True)
    channel = deliver(components, message, attempt=MAX_ATTEMPTS)
    assert channel.nacks == [(1, False)] and channel.published == []  # requeue=False -> DLX -> reports.dlq
    job = components.jobs.get(message["job_id"])
    assert job["status"] == "failed" and "FaultInjected" in job["error"] and job["attempts"] == MAX_ATTEMPTS


def test_duplicate_message_for_done_job_is_skipped(components):
    message = new_job(components)
    deliver(components, message)
    first_update = components.jobs.get(message["job_id"])["updated_at"]
    channel = deliver(components, message)
    assert channel.acks == [1]
    assert components.jobs.get(message["job_id"])["updated_at"] == first_update
