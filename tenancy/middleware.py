import jwt
from django.conf import settings

from .context import reset_current_tenant_id, set_current_tenant_id
from .models import Tenant


class TenantMiddleware:
    """Resolves the current tenant for the full request lifecycle.

    Resolution order:
      1. `Authorization: Bearer <jwt>` with a `tenant_id` claim.
      2. Subdomain of the Host header, e.g. `acme.example.com` -> "acme".

    The tenant id is stashed in a contextvars.ContextVar (tenancy.context)
    at the start of the request and reset in a finally block, so it never
    leaks into the next request handled by the same worker thread -- and,
    per ANSWERS.md, the ContextVar (rather than threading.local) is what
    keeps this correct once the app moves to async views.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = self._resolve_tenant_id(request)
        token = set_current_tenant_id(tenant_id)
        try:
            response = self.get_response(request)
        finally:
            reset_current_tenant_id(token)
        return response

    def _resolve_tenant_id(self, request):
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token_str = auth_header.removeprefix("Bearer ").strip()
            try:
                payload = jwt.decode(
                    token_str, settings.TENANT_JWT_SECRET, algorithms=["HS256"]
                )
            except jwt.PyJWTError:
                return None
            tenant_id = payload.get("tenant_id")
            if tenant_id is not None:
                return tenant_id

        host = request.META.get("HTTP_HOST", "").split(":")[0]
        subdomain = host.split(".")[0] if "." in host else None
        if subdomain:
            tenant = Tenant.objects.filter(subdomain=subdomain).first()
            if tenant:
                return tenant.id

        return None
