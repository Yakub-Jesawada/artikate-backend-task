"""Current-tenant context, backed by contextvars.ContextVar rather than
threading.local.

See ANSWERS.md Section 3 (async question) for the full reasoning. Short
version: asyncio can run many coroutines on one OS thread, and Django's
sync_to_async can hop a single logical request across multiple threads from
a thread pool. A threading.local set at the start of a request can therefore
leak into a concurrent request sharing the same thread, or vanish when the
request hops threads mid-flight. contextvars.ContextVar is copied into each
new asyncio Task's context and correctly isolates concurrent requests
regardless of which OS thread executes them.
"""

import contextvars

_current_tenant_id = contextvars.ContextVar("current_tenant_id", default=None)


def set_current_tenant_id(tenant_id):
    return _current_tenant_id.set(tenant_id)


def get_current_tenant_id():
    return _current_tenant_id.get()


def reset_current_tenant_id(token):
    _current_tenant_id.reset(token)


class no_tenant_scope:
    """Explicit, auditable escape hatch for legitimate cross-tenant admin
    tooling. Bypassing scoping any other way is not possible: the default
    manager always reads from the ContextVar above.
    """

    def __enter__(self):
        self._token = set_current_tenant_id("__all__")
        return self

    def __exit__(self, exc_type, exc, tb):
        reset_current_tenant_id(self._token)
