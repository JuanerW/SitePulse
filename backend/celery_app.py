import os

from celery import Celery
from celery.signals import after_setup_logger

from observability import configure_logging


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
    # Backstop for a worker that dies without saying so (SIGKILL, OOM, host
    # reboot): Redis requeues its unacknowledged message once it is older than
    # this. It must exceed the longest legitimate job, or healthy jobs get
    # delivered twice: 200 URLs, 10 at a time, each capped at 15 s, is 300 s.
    # The fast path is the heartbeat reaper below, which notices in ~30 s.
    broker_transport_options={
        "visibility_timeout": int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "360")),
    },
    # Run with `celery worker -B`; one worker on one host needs no separate beat.
    beat_schedule={
        "reap-stale-jobs": {"task": "sitepulse.reap_stale_jobs", "schedule": 15.0},
        "cleanup-jobs": {"task": "sitepulse.cleanup_jobs", "schedule": 600.0},
    },
    worker_hijack_root_logger=False,
    timezone="UTC",
)


@after_setup_logger.connect
def configure_worker_logging(**_: object) -> None:
    """Use the same JSON log format after Celery initializes logging."""

    configure_logging()
