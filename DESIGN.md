# DESIGN.md — Section 2: Rate-Limited Async Job Queue

## Architecture choice

| Option | Trade-offs |
|---|---|
| **Celery + Redis** (chosen) | Mature retry/backoff (`autoretry_for`, `retry_backoff`), pluggable brokers, `acks_late`/`task_reject_on_worker_lost` give an explicit, well-documented answer to "what happens on SIGKILL". Heavier operationally (needs a running worker + broker process) than the alternatives. |
| **Django-Q(2)** | Simpler to operate (broker can be the Django ORM itself, no separate worker infra strictly required), decent retry support. Smaller ecosystem, less battle-tested at high throughput, and Redis-backed atomic rate limiting still has to be hand-rolled either way — so it doesn't remove the hardest part of this task. |
| **Custom (threading/asyncio queue in-process)** | No extra infrastructure. But loses jobs on process restart unless durability is re-implemented from scratch (a durable queue *is* what Celery+Redis already gives you), and reinvents retry/backoff/worker-pool semantics that Celery has already solved. Reasonable for a single-process toy, wrong choice for "must not lose jobs if the worker crashes". |

**Chosen: Celery + Redis.** It directly gives durable delivery (broker persists the message until acked), a standard retry/backoff API, and — critically — a documented, testable answer to the SIGKILL question via `task_acks_late` + `task_reject_on_worker_lost`. The added operational cost (running `celery -A config worker`) is small relative to what it buys.

## Rate limiter: sliding window (Redis sorted set + `ZREMRANGEBYSCORE`)

Implementation: `emailqueue/rate_limiter.py::SlidingWindowRateLimiter`.

**1. Why this approach over token-bucket / fixed-window**

- **Fixed window** (`INCR` + `EXPIRE`) is simplest, but allows up to `2x` the limit right at a window boundary — e.g. 200 sends at `59.9s` and another 200 at `60.1s` is 400 in ~200ms of wall-clock time. For a provider that will ban the account on abuse, that boundary burst is exactly the failure mode we're trying to avoid.
- **Token bucket** (`DECR` + `TTL`) smooths bursts well and is a fine choice too, but modelling refill correctly under concurrent decrements needs its own care (typically also a Lua script), and it doesn't give as direct an answer to "how many requests happened in the last 60 seconds exactly" — which is precisely what "the provider allows 200/minute" is asking for.
- **Sliding window** (sorted set keyed by timestamp) directly answers "how many requests happened in the trailing N milliseconds" with no boundary effect: entries older than the window are evicted (`ZREMRANGEBYSCORE`) before every check. This is the most literal, least-surprising implementation of "200 emails per minute" and was chosen for that reason, accepting a bit more Redis memory (one sorted-set entry per accepted send, expired members via `PEXPIRE` on the whole key) as the trade-off.

**2. Atomicity**

All three Redis operations — cleanup (`ZREMRANGEBYSCORE`), count (`ZCARD`), and conditional insert (`ZADD` + `PEXPIRE`) — are wrapped in a single Lua script (`_SLIDING_WINDOW_LUA`, executed via `redis-py`'s `register_script`/`EVALSHA`). Redis executes a script as one indivisible operation: no other client's command can interleave between the cleanup and the conditional insert, so two workers racing to acquire the last available slot cannot both "win". A `MULTI/EXEC` transaction was not used here because it cannot express the conditional logic ("only ZADD if under the limit") — `MULTI/EXEC` queues commands blindly and cannot branch on a value read earlier in the same transaction; Lua can.

**3. Redis failure: fail closed**

If Redis is unreachable, `try_acquire()` raises `RedisUnavailable`. The task (`emailqueue/tasks.py::send_transactional_email`) treats that as **fail closed**: it re-queues itself via `self.retry(countdown=2)` rather than sending un-throttled. Rationale: an email provider that enforces "200/minute" will very likely suspend or throttle the whole account if that limit is blown through during a Redis outage, which is worse for the business than a delayed batch of transactional emails. Fail-open would be the right call for a limiter guarding, say, an internal cache-warming job where "unthrottled for a few minutes" is harmless — that's not this scenario.

## Retry / dead-letter handling

- `send_transactional_email` is decorated with `autoretry_for=(EmailProviderError,)`, `retry_backoff=True`, `retry_backoff_max=30`, `retry_jitter=True`, `max_retries=5` — standard exponential backoff with jitter for genuine provider failures.
- Capacity-exhausted (`try_acquire()` returned `False`) and Redis-unavailable cases call `self.retry()` explicitly, so a rate-limited job is redelivered through Celery's broker instead of blocking the worker with a sleep.
- `DeadLetterTask.on_failure` (the task's base class) fires exactly once when Celery gives up on a task — either because `autoretry_for`'s retries are exhausted or a manual `self.retry()` exceeds `max_retries` — and records the job as `EmailJob.STATUS_DEAD_LETTERED` plus a `DeadLetter` row. Nothing is dropped silently: every job ends in `sent` or `dead_lettered`.

## SIGKILL: what happens to in-flight tasks

Three settings work together (`config/settings.py`):

```python
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_REJECT_ON_WORKER_LOST = True
```

By default, Celery acks a task **as soon as the worker receives it**, before it runs. If that worker is `SIGKILL`'d mid-task, the message is already gone from the broker — the job is lost, permanently, with no trace.

With `task_acks_late=True`, the ack is sent only **after** the task finishes (successfully or with a handled failure). If the worker process is `SIGKILL`'d while executing the task, it never gets the chance to ack, and Redis (acting as the broker) redelivers the message to another available worker once the visibility/connection is lost. `task_reject_on_worker_lost=True` makes this explicit: when Celery detects a worker died mid-task (as opposed to a clean failure), it explicitly rejects (rather than acks) the message so it is requeued instead of silently dropped. `worker_prefetch_multiplier=1` limits how many unacked tasks a single worker can be holding at once — without it, a worker could have several tasks prefetched into memory when it's killed, all of which are equally at risk of being lost/duplicated on redelivery; setting it to 1 minimizes the blast radius of any single worker crash to one in-flight task.

The trade-off this introduces: a task that crashes the worker itself (e.g. a segfault) partway through a side effect (e.g. it already called the real email provider, just before being killed) will be redelivered and could send the same email twice. This implementation accepts "at-least-once" delivery, not "exactly-once" — a genuinely idempotent provider call (e.g. an idempotency key sent to the provider) would be needed to close that gap, and is out of scope here.
