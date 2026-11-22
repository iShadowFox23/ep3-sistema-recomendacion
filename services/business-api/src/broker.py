"""RabbitMQ: topología de colas de reportes y publicación de trabajos.

    exchange `reports` (direct) --report.request--> cola `reports.requests` (durable)
                                                        | rechazado tras MAX_ATTEMPTS
                                                        v
    exchange `reports.dlx` (fanout) ---------------> cola `reports.dlq` (revisión manual)

Mensajes persistentes (delivery_mode=2) y publisher confirms: si RabbitMQ no confirma, la API
responde 503 en vez de perder el trabajo en silencio.
"""
from __future__ import annotations

import json

import pika

EXCHANGE = "reports"
ROUTING_KEY = "report.request"
QUEUE = "reports.requests"
DLX = "reports.dlx"
DLQ = "reports.dlq"
MAX_ATTEMPTS = 3


class BrokerUnavailable(RuntimeError):
    pass


def declare_topology(channel) -> None:
    channel.exchange_declare(exchange=EXCHANGE, exchange_type="direct", durable=True)
    channel.exchange_declare(exchange=DLX, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=DLQ, durable=True)
    channel.queue_bind(queue=DLQ, exchange=DLX)
    channel.queue_declare(queue=QUEUE, durable=True, arguments={"x-dead-letter-exchange": DLX})
    channel.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key=ROUTING_KEY)


def publish(channel, message: dict, attempt: int = 1) -> None:
    channel.basic_publish(
        exchange=EXCHANGE,
        routing_key=ROUTING_KEY,
        body=json.dumps(message).encode("utf-8"),
        properties=pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            message_id=message["job_id"],
            headers={"x-attempt": attempt},
        ),
        mandatory=True,
    )


class Publisher:
    """Conexión corta por publicación: volumen bajo (reportes) y sin estado compartido entre hilos."""

    def __init__(self, url: str):
        self.params = pika.URLParameters(url)
        self.params.socket_timeout = 3
        self.params.blocked_connection_timeout = 3

    def publish_job(self, message: dict) -> None:
        try:
            connection = pika.BlockingConnection(self.params)
        except Exception as exc:
            raise BrokerUnavailable(str(exc)) from exc
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            declare_topology(channel)
            publish(channel, message)
        except Exception as exc:
            raise BrokerUnavailable(str(exc)) from exc
        finally:
            if connection.is_open:
                connection.close()
