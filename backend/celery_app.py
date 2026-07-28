import os

from celery import Celery


# Redis logical database 0 is used as the Celery broker.
# FastAPI writes messages here and workers consume them.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "sitepulse",
    broker=REDIS_URL,
    include=["tasks"],
)

celery_app.conf.update(
    # Only allow JSON task payloads to avoid unsafe pickle deserialization.
    task_serializer="json",
    accept_content=["json"],
    # Acknowledge after execution so a message can be redelivered if a worker exits unexpectedly.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Docker startup order is not guaranteed, so workers keep waiting for Redis.
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)
