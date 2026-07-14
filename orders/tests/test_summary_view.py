from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from rest_framework.test import APIClient

from orders.models import Customer, Order, OrderItem


class OrderSummaryQueryCountTests(TestCase):
    """Proves the fix: query count stays flat as order count grows,
    instead of scaling linearly (the N+1 regression from Section 1).
    """

    @classmethod
    def setUpTestData(cls):
        cls.customer = Customer.objects.create(name="Heavy Buyer", email="heavy@example.com")
        cls.orders = []
        for _ in range(30):
            order = Order.objects.create(customer=cls.customer)
            OrderItem.objects.create(order=order, product_name="Widget", quantity=2, unit_price=Decimal("5.00"))
            OrderItem.objects.create(order=order, product_name="Gadget", quantity=1, unit_price=Decimal("12.50"))
            cls.orders.append(order)

    def test_fixed_view_query_count_is_constant(self):
        client = APIClient()
        url = reverse("orders:summary") + f"?customer_id={self.customer.id}"

        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 30)
        # 1 query for get_object_or_404(Customer) + 1 annotated/joined query
        # for the orders themselves, regardless of how many orders there are.
        self.assertLessEqual(len(ctx.captured_queries), 3)

    def test_fixed_view_totals_are_correct(self):
        client = APIClient()
        url = reverse("orders:summary") + f"?customer_id={self.customer.id}"
        response = client.get(url)

        for row in response.data:
            self.assertEqual(row["item_count"], 2)
            self.assertEqual(Decimal(row["total"]), Decimal("22.50"))

    def test_broken_view_query_count_scales_with_orders(self):
        """Documents the regression this assessment is about: the broken
        view issues roughly 1 + 2N queries for N orders."""
        client = APIClient()
        url = reverse("orders:summary-broken") + f"?customer_id={self.customer.id}"

        with CaptureQueriesContext(connection) as ctx:
            response = client.get(url)

        self.assertEqual(response.status_code, 200)
        # 30 orders -> 1 (customer) + 1 (orders) + 30 (customer.name per row)
        # + 30 (items.count per row) + 30 (items iteration per row) = ~92
        self.assertGreater(len(ctx.captured_queries), 60)
