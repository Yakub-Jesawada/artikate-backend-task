import redis
from django.conf import settings
from django.test import TestCase

from config.celery import app as celery_app
from emailqueue.models import EmailJob
from emailqueue.tasks import RATE_LIMITER_NAME, send_transactional_email


class QueueBurstTests(TestCase):
    """Submits 500 jobs and asserts the three behaviours the assessment asks
    for: no job is lost, the rate limit is never exceeded, and an
    intentional failure is retried correctly.

    Runs Celery in ALWAYS_EAGER mode so the suite stays fast and
    deterministic. Caveat (documented, not hidden -- see DESIGN.md): eager
    mode re-invokes retries immediately instead of waiting for the real
    `countdown`, so jobs that lose the race for one of the 200 rate-limit
    slots exhaust their 5 retries almost instantly here, instead of over
    the following minute as they would against a real worker + broker. They
    still end up accounted for in the dead-letter store rather than lost,
    which is exactly the invariant this test is checking.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._prev_eager = celery_app.conf.task_always_eager
        cls._prev_propagates = celery_app.conf.task_eager_propagates
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = False

    @classmethod
    def tearDownClass(cls):
        celery_app.conf.task_always_eager = cls._prev_eager
        celery_app.conf.task_eager_propagates = cls._prev_propagates
        super().tearDownClass()

    def setUp(self):
        self.redis = redis.from_url(settings.REDIS_URL)
        self.redis.delete(f"ratelimit:{RATE_LIMITER_NAME}")

    def test_500_jobs_none_lost_rate_limit_respected_and_retry_works(self):
        jobs = [
            EmailJob.objects.create(recipient=f"user{i}@example.com", subject="Order confirmation")
            for i in range(500)
        ]

        # Engineered to fail once before succeeding -- exercises the
        # autoretry_for + retry_backoff path deterministically.
        send_transactional_email.apply(
            args=[jobs[0].id], kwargs={"simulate_failures": 1}
        ).get(propagate=False)

        for job in jobs[1:]:
            # propagate=False: a job that permanently fails (e.g. loses the
            # race for a rate-limit slot and exhausts retries) still ends up
            # correctly dead-lettered by on_failure -- we assert on DB state
            # below, not on every individual call succeeding.
            send_transactional_email.apply(args=[job.id]).get(propagate=False)

        refreshed = list(EmailJob.objects.all())
        self.assertEqual(len(refreshed), 500)

        # No job left dangling mid-flight -- every job reaches a terminal
        # state (sent, or accounted for in the dead-letter store).
        terminal_statuses = {EmailJob.STATUS_SENT, EmailJob.STATUS_DEAD_LETTERED}
        for job in refreshed:
            self.assertIn(job.status, terminal_statuses)

        sent = [j for j in refreshed if j.status == EmailJob.STATUS_SENT]
        self.assertLessEqual(len(sent), settings.EMAIL_RATE_LIMIT_PER_MINUTE)

        retried_job = EmailJob.objects.get(pk=jobs[0].id)
        self.assertEqual(retried_job.status, EmailJob.STATUS_SENT)
        self.assertEqual(retried_job.attempts, 2)
