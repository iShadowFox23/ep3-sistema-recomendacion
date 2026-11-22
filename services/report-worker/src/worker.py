"""report-worker: consume la cola `reports.requests` de RabbitMQ.

Manual ack, prefetch=1; tras 3 fallos el mensaje va a `reports.dlq` (dead-letter exchange).
Ejecuta la consulta en Athena, escribe el CSV en S3 y actualiza el job en DynamoDB (`report_jobs`).
"""
import os

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
QUEUE = "reports.requests"


def main() -> None:
    # TODO: conectar con pika, declarar exchange/colas y consumir
    print(f"report-worker listo (cola={QUEUE})", flush=True)


if __name__ == "__main__":
    main()
