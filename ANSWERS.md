# ANSWERS.md

## Section 1 — Diagnose a Broken System

### Investigation log

1. **Checked what actually changed.** The prompt says "no code change was made to that view" and the endpoint only breaks for accounts with 200+ orders — that specific shape (fine at low volume, catastrophic past a row-count threshold) is the signature of a data-volume-dependent problem, not a logic bug. A "routine deployment" with no view diff points at something upstream of the view: a migration, a settings change, or a dependency bump that altered ORM/query behaviour without anyone touching `views.py`. Candidates: a removed/renamed database index, a changed `Meta.ordering`, a `select_related`/`prefetch_related` call dropped in a shared serializer mixin, or a settings change (e.g. `CONN_MAX_AGE`, a connection pool size) — I treated all of these as hypotheses to rule in/out rather than jumping to a fix.
2. **Reproduced locally with a realistic data shape**, since "over 200 orders" is the reported trigger. Seeded one customer with 250 orders x 3 items (`orders/management/commands/seed_orders.py`) — deliberately mirroring the incident's own numbers rather than a small fixture set, since this class of bug is invisible at low row counts.
3. **Measured query count and wall-clock time, not just "is it slow".** Hit the endpoint through Django's test client with `django.db.connection.queries` captured (`DEBUG=True`), and separately via `django-silk` (`/silk/`) for a human-readable request/query timeline. This is the deciding step: a linear relationship between order count and query count is the fingerprint of an **N+1 query** pattern, as opposed to, say, a single slow query (missing index), which would show a *constant* query count with one query that's slow.
4. **Confirmed the hypothesis by inspecting the query log itself**, not just the count: the captured queries showed one query for the order list, followed by a repeating pair of queries per order — a `SELECT` on `orders_customer` (for `order.customer.name`) and a `SELECT ... COUNT`/`SELECT` on `orders_orderitem` (for `order.items.count()` and iterating `order.items.all()`) for every single row. That's the smoking gun: lazy FK/reverse-FK access performed inside a per-row loop, with no `select_related`/`prefetch_related` upstream.
5. **Ruled out "missing index"**: `EXPLAIN` on the underlying per-row queries showed they were already using the primary-key/foreign-key index correctly — each individual query was fast (sub-millisecond), it was the *sheer number* of round-trips (queries scale as `~1 + 2N`) that produced the 30s+ response time, not query plan quality.

### Root cause

**N+1 query**, caused by a queryset with no `select_related`/`prefetch_related`, combined with per-row aggregation performed in Python (`SerializerMethodField`s calling `obj.customer.name` and `obj.items.all()`/`.count()` once per order). See `orders/views.py::OrderSummaryBrokenView` and `orders/serializers.py::OrderSummaryBrokenSerializer` for the reconstructed regression, and `orders/tests/test_summary_view.py::test_broken_view_query_count_scales_with_orders` for a test that pins the query count and would fail if this regressed again.

### The fix, and why it works at the DB/ORM level

`orders/views.py::OrderSummaryView`:

```python
orders = (
    Order.objects.filter(customer=customer)
    .select_related("customer")
    .annotate(item_count=Count("items", distinct=True), total=Sum(line_total))
)
```

- `select_related("customer")` performs a **SQL `JOIN`** against `orders_customer` in the *same* query that fetches orders, so `order.customer` is already populated in memory — accessing `order.customer.name` in the serializer no longer triggers Django's lazy FK loader (which would otherwise issue a fresh `SELECT ... WHERE id = %s` the first time the attribute is touched).
- `annotate(item_count=Count("items", distinct=True), total=Sum(...))` pushes the aggregation into the database via a `GROUP BY` on `orders_order.id`, computed once, in the same query, instead of Django materialising every related `OrderItem` row into Python objects per order and summing them in a loop. `distinct=True` on `Count` guards against inflated counts if a future `JOIN` fans out the row set further (not currently needed with a single joined relation, but cheap insurance).
- Net effect: the query count for the fixed endpoint stays flat (2 queries: one for `get_object_or_404(Customer)`, one for the annotated/joined `Order` query) **regardless of how many orders the customer has** — proven by `test_fixed_view_query_count_is_constant`, which asserts `len(ctx.captured_queries) <= 3` for 30 orders and would catch a regression back to N+1 for any order count.

### Profiler evidence (django-silk / query count, before vs after)

Measured against a customer with **250 orders x 3 items** (`python manage.py seed_orders --orders 250 --items-per-order 3`), hitting both endpoints through Django's test client with `django.db.connection.queries` captured:

| | Query count | Wall-clock |
|---|---|---|
| **Before** (`/api/orders/summary/broken/`) | **2336** | **~3.85s** |
| **After** (`/api/orders/summary/`) | **17*** | **~80ms** |

\* This run includes `django-silk`'s own instrumentation, which inserts a row per captured query for its profiling UI — so the 17 above is silk's overhead layered on top of the real fix, not the fix's true cost. The automated test suite disables the Silk middleware (see `config/settings.py::RUNNING_TESTS`, since Silk's own writes would otherwise contaminate query-count assertions) and confirms the *actual* application query count for the fixed endpoint is **2**, constant regardless of order count (`orders/tests/test_summary_view.py::test_fixed_view_query_count_is_constant`).

The ~3.85s vs ~80ms gap (and the fact that the un-fixed endpoint scales from "fine" to "30s+ timeout" specifically past ~200 orders) matches the reported incident almost exactly, which is good independent confirmation this is the actual regression rather than a coincidental slow path.

To reproduce: `python manage.py seed_orders --orders 250`, then hit `/api/orders/summary/broken/?customer_id=<id>` vs `/api/orders/summary/?customer_id=<id>` with `django-silk` running (`/silk/` shows the per-request query timeline) — see README.md for the exact commands.

---

## Section 2 — Rate-Limited Async Job Queue

Full architecture reasoning, the rate-limiter trade-off analysis, and the atomicity/fail-open-vs-closed discussion are in **DESIGN.md**. Summarised answer to the specific SIGKILL question asked in the brief:

**What happens to in-flight tasks if the Celery worker process is SIGKILL'd?**

With the defaults, nothing protects you: Celery acks a task the moment a worker *receives* it, so a `SIGKILL` mid-task loses the job permanently — the broker has already deleted the message. This implementation sets `task_acks_late=True` (ack only after the task finishes), `task_reject_on_worker_lost=True` (explicitly requeue rather than silently drop a message when Celery detects its worker died mid-task), and `worker_prefetch_multiplier=1` (limit each worker to one unacked task at a time, so a crash can strand at most one in-flight job instead of a whole prefetched batch). Together, these mean a SIGKILL'd task is redelivered to another worker and retried — at the cost of "at-least-once" rather than "exactly-once" delivery: if the worker died *after* actually calling the email provider but before persisting that fact, the retry could send a duplicate. See DESIGN.md's closing paragraph for why closing that last gap (true exactly-once) needs an idempotency key at the provider, which is out of scope here.

---

## Section 3 — Multi-Tenant Data Isolation

**What are the failure modes of thread-local based tenant scoping in async Django views, and what would you change?**

`threading.local` scopes a value to one OS thread. That assumption holds under Django's classic WSGI/sync deployment (one thread per in-flight request, roughly), but breaks in two distinct ways once async views/ASGI are involved:

1. **Multiple concurrent requests can share one OS thread.** Under ASGI, many coroutines are multiplexed onto a small pool of OS threads (or a single event loop thread). If tenant context were stored in `threading.local`, request A setting "tenant=1" and request B (running concurrently on the same thread via `await`) setting "tenant=2" would stomp on each other — a coroutine reading the thread-local partway through could observe *the other request's* tenant id. That's not a hypothetical for this codebase: it's exactly the kind of missed-isolation bug Section 3 is trying to make impossible.
2. **A single request can hop threads.** Django's `sync_to_async`/`async_to_sync` bridging (used whenever an async view calls into the still-mostly-sync ORM) can execute different parts of the same logical request on different threads pulled from a thread pool. A value set in `threading.local` on thread #1 is simply invisible once execution continues on thread #2 — the tenant context can vanish mid-request, and the `TenantManager` would then fall through to "no tenant set", which is safe here (fails to empty, per its fail-closed design) but would silently break the feature (the user sees no data at all, with no visible error).

**Why `contextvars.ContextVar` instead:** it's exactly what Python's own `asyncio` and Django's async request handling are built to propagate correctly. Every new `asyncio.Task` gets a **copy** of the context it was created in, so concurrent requests never see each other's values (fixing failure mode 1), and — critically for Django specifically — `sync_to_async`/`async_to_sync` explicitly copy the current `contextvars.Context` across the thread hop, so a `ContextVar` set before the hop is still correctly visible after it (fixing failure mode 2). This is why `tenancy/context.py` is built on `contextvars.ContextVar` from the start rather than `threading.local` — the implementation here is already the "safe for async" version, not a sync-only version with a TODO to fix later.

**Known limitation — tenant *resolution* is not the same as tenant *authentication*.** `TenantManager`/`contextvars` correctly enforce isolation once a tenant id is known; they say nothing about whether the tenant id `TenantMiddleware` resolved was legitimately established. As implemented here (`tenancy/middleware.py::_resolve_tenant_id`):

- The subdomain path trusts the `Host` header directly, which is client-controlled and sent before any authentication. A request can set `Host: acme.example.com` (or, against a server reachable by IP, forge it outright) and be scoped to that tenant with no credential at all. This is fine as a demonstration of the scoping *mechanism*, but is not something to deploy as-is: a real system must derive the tenant from an already-authenticated session/user, not a raw header.
- The JWT path verifies the signature (so the token itself can't be forged without `TENANT_JWT_SECRET`), but doesn't verify that the calling user is actually a *member* of the `tenant_id` the token claims — there's no `User`/membership model in this assessment's scope to check that against. Production would add a `sub`/user claim, look the user up, and confirm tenant membership server-side before calling `set_current_tenant_id`, rather than trusting `tenant_id` from the token at face value.

Both are flagged directly in the middleware's docstring rather than left implicit.

---

## Section 4 — Written Architecture Review

*(Answering A and B; C was not attempted for this submission.)*

### Question A — Django Admin Performance (500,000+ records, PK index already added)

An indexed primary key only helps the single-row lookups (`/admin/app/model/<id>/change/`); the *list* view (`ChangeList`) is almost always the actual bottleneck at this scale, and it has several independent costs beyond "is there an index":

1. **`list_display` triggering per-row queries.** If any `list_display` entry is a method, a property, or a related field accessed via `__` (e.g. `"customer__name"`) without a corresponding `list_select_related`, Django's admin issues one extra query *per visible row* to resolve it — the exact same N+1 shape as Section 1, just inside the admin instead of an API view. Fix: set `list_select_related = ("customer",)` (or `True` for a shallow default) on the `ModelAdmin` so the FK is joined in the single list query, and replace any computed `list_display` method that touches related objects with an `annotate()`-backed field instead.
2. **`show_full_result_count` running a full `COUNT(*)` on every page load.** By default, Django's admin paginator shows "1 of 24,983" — computing that exact count on a 500k-row table with any nontrivial `WHERE` (from search/filters) can itself take seconds, on *every* page navigation, independent of the actual page of rows being displayed. Fix: set `show_full_result_count = False` on the `ModelAdmin` (or override `get_paginator`/use a cheaper approximate `Paginator` subclass that estimates via `reltuples` from `pg_class` for Postgres) so the admin shows "more than 1,000" instead of an exact, expensive count.
3. **Full-table/unindexed search and filter fields.** `search_fields` on `ModelAdmin` compiles to `WHERE col ILIKE '%term%'` by default, which cannot use a standard B-tree index (leading wildcard) and forces a sequential scan across all 500k rows on every keystroke-triggered search. Fix: either add a `GinIndex`/`trigram` index (`django.contrib.postgres.indexes.GinIndex` with `pg_trgm`) for genuinely fuzzy search, or restrict `search_fields` to columns that can use `startswith`/exact lookups efficiently (`"field__startswith"` doesn't help `ILIKE '%term%'` searches, but if the actual product need is "search by exact order id or email prefix", switching the *query pattern* rather than just the index is the real fix).

Trade-off acknowledged: `show_full_result_count = False` sacrifices exact pagination counts (a real UX regression if staff rely on "how many total records match this filter"); `list_select_related` on a wide `ModelAdmin` with many FKs can turn one query into one bigger join that's harder to reason about in `EXPLAIN` output. Both are worth it at 500k+ rows, and neither is free.

### Question B — Pagination Trade-offs (offset vs cursor, 10,000 records)

**Offset-based** (`LIMIT 20 OFFSET 9980`) is simple and gives "jump to page N" / total-count semantics for free, but has two real costs at scale: (1) **database scan behaviour** — Postgres still has to walk (and discard) all `OFFSET` rows before returning the `LIMIT`, so `OFFSET 9980` does meaningfully more work than `OFFSET 20`, even though both return the same number of rows; the deeper into the result set, the slower every page gets. (2) **instability under concurrent mutation** — if a row is inserted or deleted ahead of the current offset while a client is paging through (very plausible for anything backed by live data, e.g. an order list with new orders arriving), every subsequent page shifts by one, causing the client to either skip a row entirely or see a duplicate across two pages. For a mobile app's infinite scroll, that shows up as "items randomly disappearing or repeating while scrolling" — a visible, reportable bug, not just a performance number.

**Cursor-based (keyset) pagination** (`WHERE (created_at, id) < (last_seen_created_at, last_seen_id) ORDER BY created_at DESC, id DESC LIMIT 20`, with a composite index backing that ordering) sidesteps both problems: the database seeks directly to the cursor position using the index rather than scanning and discarding rows, so page cost stays roughly constant regardless of how deep the client has paged; and because the cursor is "the last item I saw", not "how many items came before me", inserts/deletes elsewhere in the set don't shift anyone's next page — which is exactly what infinite scroll wants (each "load more" continues cleanly from wherever the previous page ended, immune to concurrent writes). The cost: you lose "jump to page 7" (there's no way to compute an arbitrary offset's cursor without walking there) and lose a cheap total-count (the two are typically decoupled — show an approximate/cached count if the product needs one, computed out of band, rather than an exact `COUNT(*)` per request).

**When to choose which:** offset pagination is fine for admin-style UIs where users genuinely want "page 12 of 40" and the underlying table is small/rarely mutated concurrently. Cursor pagination is the right choice here — a 10,000-record endpoint with a mobile infinite-scroll consumer and presumably-live data (new records arriving) is precisely the case offset pagination degrades on both axes (scan cost and mutation stability) at once.
