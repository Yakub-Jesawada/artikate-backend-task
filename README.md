# Artikate Studio — Backend Developer Assessment

Django/DRF backend covering the four required sections of the assessment:

1. **Diagnose a broken system** — N+1 query regression on an orders summary endpoint (`orders/`)
2. **Rate-limited async job queue** — Celery + Redis, atomic sliding-window rate limiter, retries + dead-letter (`emailqueue/`)
3. **Multi-tenant data isolation** — automatic ORM-level tenant scoping via `contextvars` (`tenancy/`)
4. **Written architecture review** — `ANSWERS.md`

Written reasoning: **`ANSWERS.md`** (all sections) and **`DESIGN.md`** (Section 2 architecture).

Section 5 (optional Loom recording) was **not recorded** for this submission — everything it would demonstrate can be run directly with the commands below (seed 500+ jobs, watch the Redis queue/rate-limiter state, and see a retry/dead-letter happen), but no video is included.

## Requirements

- Python 3.11+
- Docker + Docker Compose (for Postgres + Redis only — the Django app and Celery worker run directly on your machine, no app image to build)

## Setup (should take under 5 minutes)

```bash
git clone <this-repo-url>
cd artikate-backend-task

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # defaults work as-is; Postgres/Redis are mapped to
                        # non-default host ports (5433/6380) to avoid
                        # clashing with any Postgres/Redis you already run
                        # locally -- edit .env if you'd rather use 5432/6379

docker compose up -d   # starts Postgres and Redis only
python manage.py migrate
python manage.py test  # all tests should pass
```

## Running the app

```bash
python manage.py runserver
```

- Admin: http://localhost:8000/admin/ (run `python manage.py createsuperuser` first)
- django-silk profiler UI: http://localhost:8000/silk/

## Section 1 — reproducing the N+1 regression

```bash
python manage.py seed_orders --orders 250 --items-per-order 3
```

Then compare, with the server running:

```bash
curl "http://localhost:8000/api/orders/summary/broken/?customer_id=1"  # the regression
curl "http://localhost:8000/api/orders/summary/?customer_id=1"         # the fix
```

Open http://localhost:8000/silk/ to see the query count/timeline for each request. See `ANSWERS.md` Section 1 for the full investigation log and the measured before/after numbers (2336 queries / ~3.85s -> 2 queries / ~80ms for 250 orders).

## Section 2 — running the job queue

In one terminal, start a worker:

```bash
celery -A config worker --loglevel=info
```

In another, submit jobs via the Django shell:

```bash
python manage.py shell -c "
from emailqueue.models import EmailJob
from emailqueue.tasks import send_transactional_email

for i in range(120):
    job = EmailJob.objects.create(recipient=f'user{i}@example.com', subject='Order confirmation')
    send_transactional_email.delay(job.id)
"
```

Watch it drain, respecting the 200/minute limit:

```bash
redis-cli -p 6380 zcard ratelimit:transactional-email   # current window occupancy
python manage.py shell -c "
from emailqueue.models import EmailJob
print(EmailJob.objects.values('status').annotate(n=__import__('django.db.models', fromlist=['Count']).Count('id')))
"
```

To see a retry + dead-letter happen on purpose:

```bash
python manage.py shell -c "
from emailqueue.models import EmailJob
from emailqueue.tasks import send_transactional_email
job = EmailJob.objects.create(recipient='flaky@example.com', subject='Retry demo')
send_transactional_email.delay(job.id, simulate_failures=1)
"
```

Watch the worker terminal: you'll see the first attempt raise the simulated provider error, an exponential-backoff retry get scheduled, and the second attempt succeed. Set `simulate_failures=10` (above `max_retries=5`) instead to see it exhaust retries and land in `EmailJob.objects.filter(status="dead_lettered")` / the `DeadLetter` table.

## Section 3 — multi-tenant isolation

```bash
python manage.py shell -c "
from tenancy.models import Tenant, Order
a = Tenant.objects.create(name='Acme', subdomain='acme')
b = Tenant.objects.create(name='Globex', subdomain='globex')
Order.objects.all()  # empty: no tenant context outside a request -- fails safe, not open
"
curl -H "Host: acme.example.com" http://localhost:8000/api/tenancy/orders/
```

## Running tests

```bash
python manage.py test
```

Runs the full suite (orders N+1 regression tests, rate limiter + 500-job queue burst test, tenant isolation tests). No external network access required beyond the local Postgres/Redis containers.

## Notes / things not implemented

- Section 4 answers only Q A and Q B (of A/B/C) as the brief allows answering any two.
- The email "send" itself is simulated (see `emailqueue/tasks.py`) — no real provider integration, since the assessment is about the queue/rate-limiter mechanics, not a specific ESP's API.
- The 500-job queue test runs Celery in eager mode for speed/determinism; see the docstring on `emailqueue/tests/test_queue_burst.py` for the one behavioural difference that introduces (immediate vs delayed retries) and why it doesn't affect what the test actually proves.
