import jwt
from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from tenancy.context import (
    get_current_tenant_id,
    no_tenant_scope,
    reset_current_tenant_id,
    set_current_tenant_id,
)
from tenancy.models import Order, Tenant


def make_jwt(tenant_id):
    return jwt.encode({"tenant_id": tenant_id}, settings.TENANT_JWT_SECRET, algorithm="HS256")


class TenantManagerIsolationTests(TestCase):
    """Proves the negative: tenant A's context can never observe tenant B's
    rows through the default manager, and .all() does not bypass scoping.
    """

    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", subdomain="acme")
        cls.tenant_b = Tenant.objects.create(name="Globex", subdomain="globex")
        cls.order_a = Order.objects.create(tenant=cls.tenant_a, reference="A-1", amount=10)
        cls.order_b = Order.objects.create(tenant=cls.tenant_b, reference="B-1", amount=20)

    def test_manager_scopes_to_current_tenant(self):
        token = set_current_tenant_id(self.tenant_a.id)
        try:
            results = list(Order.objects.all())
        finally:
            reset_current_tenant_id(token)

        self.assertEqual(results, [self.order_a])

    def test_tenant_b_cannot_see_tenant_a_via_all(self):
        token = set_current_tenant_id(self.tenant_b.id)
        try:
            results = list(Order.objects.all())
            ids = {o.id for o in results}
        finally:
            reset_current_tenant_id(token)

        self.assertNotIn(self.order_a.id, ids)
        self.assertEqual(ids, {self.order_b.id})

    def test_filter_get_and_count_all_respect_scoping(self):
        token = set_current_tenant_id(self.tenant_a.id)
        try:
            self.assertEqual(Order.objects.count(), 1)
            self.assertEqual(Order.objects.filter(reference="A-1").count(), 1)
            self.assertEqual(Order.objects.filter(reference="B-1").count(), 0)
            with self.assertRaises(Order.DoesNotExist):
                Order.objects.get(reference="B-1")
        finally:
            reset_current_tenant_id(token)

    def test_no_tenant_context_returns_empty_not_everything(self):
        # Fail-safe default: forgetting to set a tenant must never leak data.
        self.assertIsNone(get_current_tenant_id())
        self.assertEqual(list(Order.objects.all()), [])

    def test_explicit_bypass_is_the_only_way_to_see_everything(self):
        with no_tenant_scope():
            results = list(Order.objects.all())
        self.assertEqual(len(results), 2)


class TenantMiddlewareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name="Acme", subdomain="acme")
        cls.tenant_b = Tenant.objects.create(name="Globex", subdomain="globex")
        Order.objects.create(tenant=cls.tenant_a, reference="A-1", amount=10)
        Order.objects.create(tenant=cls.tenant_b, reference="B-1", amount=20)

    def test_jwt_header_resolves_tenant(self):
        client = APIClient()
        token = make_jwt(self.tenant_a.id)
        response = client.get(
            reverse("tenancy:orders"), HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(response.status_code, 200)
        refs = {row["reference"] for row in response.data}
        self.assertEqual(refs, {"A-1"})

    def test_different_tenant_jwt_sees_only_its_own_data(self):
        client = APIClient()
        token = make_jwt(self.tenant_b.id)
        response = client.get(
            reverse("tenancy:orders"), HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        refs = {row["reference"] for row in response.data}
        self.assertEqual(refs, {"B-1"})

    def test_subdomain_resolves_tenant_when_no_jwt_present(self):
        client = APIClient()
        response = client.get(reverse("tenancy:orders"), HTTP_HOST="acme.example.com")
        refs = {row["reference"] for row in response.data}
        self.assertEqual(refs, {"A-1"})

    def test_tenant_context_does_not_leak_across_requests(self):
        client = APIClient()
        token = make_jwt(self.tenant_a.id)
        client.get(reverse("tenancy:orders"), HTTP_AUTHORIZATION=f"Bearer {token}")

        # No auth header and a host with no matching tenant this time --
        # if the previous request's tenant context leaked, this would
        # incorrectly return tenant A's data instead of an empty list.
        response = client.get(reverse("tenancy:orders"), HTTP_HOST="unknown.example.com")
        self.assertEqual(response.data, [])
