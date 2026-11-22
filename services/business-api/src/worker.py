"""report-worker: consume `reports.requests` de RabbitMQ y genera los reportes batch.

Se despliega con la misma imagen que business-api (comparte jobs, analítica y almacenamiento)
pero como proceso y servicio ECS separado: `python -m src.worker`. Así escala por su cuenta y un
reporte pesado nunca bloquea a la API.

Garantías:
- prefetch=1 y ack manual: un mensaje solo se elimina cuando el reporte quedó guardado.
- Reintentos: si falla, se republica con `x-attempt + 1`; al agotar MAX_ATTEMPTS (3) se rechaza
  sin reencolar y RabbitMQ lo envía a `reports.dlq` (dead-letter exchange) para revisión manual.
- Idempotencia: si el trabajo ya está `done` (mensaje duplicado), se confirma sin rehacerlo.
"""
from __future__ import annotations

import json
import logging
import time

import pika

from .broker import MAX_ATTEMPTS, QUEUE, declare_topology, publish
from .reports import FaultInjected, generate
from .settings import Components, build_components, env, fault_injection_allowed

logging.basicConfig(level=logging.INFO, format='{"level":"%(levelname)s","service":"report-worker","msg":%(message)s}')
log = logging.getLogger("report-worker")


def process(comps: Components, message: dict, attempt: int) -> str:
    """Genera y guarda un reporte. Devuelve 'done' o 'skipped' (duplicado)."""
    job_id = message["job_id"]
    job = comps.jobs.get(job_id)
    if job and job.get("status") == "done":
        return "skipped"
    comps.jobs.update(job_id, status="running", attempts=attempt)
    if message["params"].get("force_error") and fault_injection_allowed():
        raise FaultInjected("falla provocada para probar reintentos y DLQ")
    start = time.perf_counter()
    frame = generate(message["report_type"], message["params"], comps.analytics, comps.catalog)
    location = comps.reports.save(job_id, frame.to_csv(index=False))
    comps.jobs.update(
        job_id, status="done", rows=int(len(frame)), location=location, error=None,
        duration_ms=int((time.perf_counter() - start) * 1000),
    )
    return "done"


def handle(comps: Components, channel, method, properties, body: bytes) -> None:
    message = json.loads(body)
    attempt = int((properties.headers or {}).get("x-attempt", 1))
    job_id = message.get("job_id")
    try:
        outcome = process(comps, message, attempt)
        channel.basic_ack(delivery_tag=method.delivery_tag)
        log.info('{"job_id":"%s","outcome":"%s","attempt":%d}', job_id, outcome, attempt)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"[:300]
        if attempt < MAX_ATTEMPTS:
            comps.jobs.update(job_id, status="retrying", attempts=attempt, error=error)
            time.sleep(min(2 ** attempt, 10))  # backoff simple antes de reencolar
            publish(channel, message, attempt=attempt + 1)
            channel.basic_ack(delivery_tag=method.delivery_tag)
            log.warning('{"job_id":"%s","retry":%d,"error":"%s"}', job_id, attempt + 1, error)
        else:
            comps.jobs.update(job_id, status="failed", attempts=attempt, error=error)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)  # -> reports.dlq
            log.error('{"job_id":"%s","dead_lettered":true,"attempts":%d,"error":"%s"}', job_id, attempt, error)


def main() -> None:
    comps = build_components()
    params = pika.URLParameters(env("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/"))
    params.heartbeat = 60
    while True:  # reconexión: RabbitMQ puede reiniciarse o tardar en levantar
        try:
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            declare_topology(channel)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=QUEUE, on_message_callback=lambda ch, m, p, b: handle(comps, ch, m, p, b))
            log.info('"escuchando cola %s"', QUEUE)
            channel.start_consuming()
        except pika.exceptions.AMQPConnectionError as exc:
            log.warning('"sin conexión a RabbitMQ (%s); reintento en 5 s"', type(exc).__name__)
            time.sleep(5)


if __name__ == "__main__":
    main()
