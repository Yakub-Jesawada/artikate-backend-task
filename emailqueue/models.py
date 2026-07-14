from django.db import models


class EmailJob(models.Model):
    """Tracks outcomes for the test suite / demo — not required for Celery
    itself to function, but gives us something durable to assert against
    ("no job is lost") independent of the Celery result backend.
    """

    STATUS_PENDING = "pending"
    STATUS_SENT = "sent"
    STATUS_DEAD_LETTERED = "dead_lettered"

    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SENT, "Sent"),
        (STATUS_DEAD_LETTERED, "Dead lettered"),
    ]

    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"EmailJob({self.recipient}, {self.status})"


class DeadLetter(models.Model):
    """Permanently-failed jobs land here after retries are exhausted."""

    job = models.OneToOneField(EmailJob, on_delete=models.CASCADE, related_name="dead_letter")
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"DeadLetter(job={self.job_id})"
