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

    SECURITY NOTE (known limitation, see ANSWERS.md Section 3): both
    resolution paths here demonstrate the ORM-scoping *mechanism* only and
    are not a real authentication/authorization layer.
      - The subdomain path trusts `Host`, which is client-controlled and
        unauthenticated -- a request can set any Host header and be treated
        as that tenant with no credential at all. A real deployment must
        resolve tenant identity from an authenticated session/user, not a
        raw header.
      - The JWT path verifies the token's signature but does not check that
        the authenticated principal is actually a member of `tenant_id` --
        there is no User/membership model in this assessment's scope to
        check that against. Production would look up the user from the
        token's subject claim and confirm tenant membership server-side
        before calling set_current_tenant_id.
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

        # Demo-only fallback: Host is client-controlled and unauthenticated
        # (see the SECURITY NOTE above). Do not treat this as an access
        # control decision in a real deployment.
        host = request.META.get("HTTP_HOST", "").split(":")[0]
        subdomain = host.split(".")[0] if "." in host else None
        if subdomain:
            tenant = Tenant.objects.filter(subdomain=subdomain).first()
            if tenant:
                return tenant.id

        return None
