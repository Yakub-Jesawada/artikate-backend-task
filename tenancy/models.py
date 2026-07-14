from django.db import models

from .context import get_current_tenant_id


class Tenant(models.Model):
    name = models.CharField(max_length=255)
    subdomain = models.SlugField(max_length=63, unique=True)

    def __str__(self):
        return self.name


class TenantManager(models.Manager):
    """Automatically scopes every queryset to the current tenant.

    Fail-safe by default: if no tenant context has been set at all (e.g. a
    background job forgot to establish one), queries return nothing rather
    than silently returning every tenant's rows. The only way to see all
    tenants' data is the explicit `tenancy.context.no_tenant_scope()`
    context manager, which is deliberately loud and easy to grep for.
    """

    def get_queryset(self):
        tenant_id = get_current_tenant_id()

        if tenant_id == "__all__":
            return super().get_queryset()

        if tenant_id is None:
            return super().get_queryset().none()

        return super().get_queryset().filter(tenant_id=tenant_id)


class Order(models.Model):
    """Demo tenant-scoped model for Section 3 (kept separate from
    orders.Order, which belongs to the unrelated Section 1 scenario).
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    reference = models.CharField(max_length=64)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    objects = TenantManager()
    # Only for legitimate cross-tenant admin tooling -- bypasses scoping
    # entirely and should be audited whenever it's used.
    all_objects = models.Manager()

    def __str__(self):
        return f"Order({self.reference}, tenant={self.tenant_id})"
