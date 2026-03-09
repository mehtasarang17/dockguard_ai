"""
Celery application configuration.

Broker: Redis (running as a Docker service).
Tasks are defined in tasks.py.
"""
import os
from celery import Celery

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', 'redis://doc-analyzer-redis:6379/0')
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', CELERY_BROKER_URL)

celery_app = Celery(
    'doc_analyzer',
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=['tasks'],
)

celery_app.conf.update(
    # Serialization
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',

    # Timezone
    timezone='UTC',
    enable_utc=True,

    # Per-worker concurrency — limits how many analyses run at once per worker.
    # Low value prevents overwhelming the LLM API with concurrent requests.
    worker_concurrency=2,

    # Prefetch — only grab 1 task at a time so tasks are distributed fairly.
    worker_prefetch_multiplier=1,

    # Task acks — acknowledge after completion (safer for long-running tasks).
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Retry policy for broker connection
    broker_connection_retry_on_startup=True,

    # Task time limits (10 minute soft, 15 minute hard for large docs)
    task_soft_time_limit=600,
    task_time_limit=900,
)
