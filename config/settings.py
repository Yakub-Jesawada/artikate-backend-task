"""
Django settings for the Artikate Studio backend assessment.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# django-silk records every query it sees into its own tables, which would
# otherwise inflate the exact query-count assertions in orders/tests. It's a
# manual profiling aid for Section 1's before/after evidence, not something
# the automated suite needs.
RUNNING_TESTS = "test" in sys.argv

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-local-dev-only-do-not-use-in-production",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "silk",
    "orders",
    "emailqueue",
    "tenancy",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "tenancy.middleware.TenantMiddleware",
]

if not RUNNING_TESTS:
    MIDDLEWARE.insert(-1, "silk.middleware.SilkyMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "artikate"),
        "USER": os.environ.get("POSTGRES_USER", "artikate"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "artikate"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Redis / Celery -------------------------------------------------------

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/0"

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# See ANSWERS.md Section 2 (SIGKILL question) for why these three settings
# matter together: acks_late + prefetch=1 + reject_on_worker_lost is what
# guarantees an in-flight job is redelivered rather than lost when a worker
# is SIGKILL'd mid-task.
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_ACKS_ON_FAILURE_OR_TIMEOUT = False

# Email provider rate limit (see emailqueue/rate_limiter.py + DESIGN.md)
EMAIL_RATE_LIMIT_PER_MINUTE = int(os.environ.get("EMAIL_RATE_LIMIT_PER_MINUTE", "200"))

# Symmetric secret used only to sign/verify the demo tenant JWTs in this
# assessment (see tenancy/middleware.py). Not used for anything else.
TENANT_JWT_SECRET = os.environ.get("TENANT_JWT_SECRET", "dev-only-tenant-jwt-secret")

# django-silk: keep it lightweight for this assessment
SILKY_PYTHON_PROFILER = False
SILKY_AUTHENTICATION = False
SILKY_AUTHORISATION = False

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
}
