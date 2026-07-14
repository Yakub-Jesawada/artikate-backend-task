import logging

from celery import Task

from config.celery import app
from .models import DeadLetter, EmailJob
from .rate_limiter import RedisUnavailable, SlidingWindowRateLimiter

logger = logging.getLogger(__name__)

RATE_LIMITER_NAME = "transactional-email"


class EmailProviderError(Exception):
    """Simulated failure from the third-party email provider."""


class DeadLetterTask(Task):
    """Base task class: when Celery gives up on a task (autoretry_for
    exhausted, or an explicit self.retry() exceeds max_retries), on_failure
    fires exactly once and we record the job as dead-lettered instead of
    letting it vanish silently.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = args[0] if args else kwargs.get("job_id")
        try:
            job = EmailJob.objects.get(pk=job_id)
        except EmailJob.DoesNotExist:
            return

        job.status = EmailJob.STATUS_DEAD_LETTERED
        job.last_error = str(exc)
        job.save(update_fields=["status", "last_error", "updated_at"])
        DeadLetter.objects.get_or_create(job=job, defaults={"reason": str(exc)})
        logger.error("Job %s dead-lettered: %s", job_id, exc)


@app.task(
    bind=True,
    base=DeadLetterTask,
    autoretry_for=(EmailProviderError,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=5,
)
def send_transactional_email(self, job_id, simulate_failures=0):
    """Send one transactional email, respecting the shared rate limit.

    simulate_failures: number of times this job should raise
    EmailProviderError before succeeding -- used by tests to exercise the
    retry/backoff/dead-letter path deterministically.
    """
    job = EmailJob.objects.get(pk=job_id)

    limiter = SlidingWindowRateLimiter(RATE_LIMITER_NAME)
    try:
        allowed = limiter.try_acquire()
    except RedisUnavailable as exc:
        # Fail closed: an unthrottled burst against a rate-limited provider
        # risks the whole account getting banned, which is worse than a
        # delayed send. Re-queue via Celery (not a blocking sleep) and try
        # again shortly once Redis is back.
        raise self.retry(exc=exc, countdown=2)

    if not allowed:
        # Capacity exhausted for the current window. Re-queue through
        # Celery's broker so this worker is free to process other jobs
        # meanwhile -- this is the "no time.sleep()" requirement in
        # practice: the wait is expressed as a scheduled redelivery, not a
        # blocking sleep in the worker process.
        raise self.retry(countdown=1)

    job.attempts += 1
    job.save(update_fields=["attempts", "updated_at"])

    if simulate_failures and job.attempts <= simulate_failures:
        job.last_error = "simulated provider 5xx"
        job.save(update_fields=["last_error", "updated_at"])
        raise EmailProviderError(f"simulated failure on attempt {job.attempts}")

    # Real send would call the provider's API here.
    job.status = EmailJob.STATUS_SENT
    job.save(update_fields=["status", "updated_at"])
    return {"job_id": job_id, "status": job.status}
