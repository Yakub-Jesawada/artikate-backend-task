from decimal import Decimal

from django.core.management.base import BaseCommand

from orders.models import Customer, Order, OrderItem


class Command(BaseCommand):
    help = "Seed a customer with 250 orders (3 items each) to reproduce the N+1 regression."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=250)
        parser.add_argument("--items-per-order", type=int, default=3)

    def handle(self, *args, **options):
        n_orders = options["orders"]
        n_items = options["items_per_order"]

        customer, _ = Customer.objects.get_or_create(
            email="heavy.buyer@example.com", defaults={"name": "Heavy Buyer"}
        )

        orders = Order.objects.bulk_create(
            [Order(customer=customer, status="completed") for _ in range(n_orders)]
        )

        items = []
        for order in orders:
            for i in range(n_items):
                items.append(
                    OrderItem(
                        order=order,
                        product_name=f"Product {i + 1}",
                        quantity=1 + (i % 3),
                        unit_price=Decimal("9.99") + Decimal(i),
                    )
                )
        OrderItem.objects.bulk_create(items)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded customer id={customer.id} with {n_orders} orders "
                f"and {len(items)} order items."
            )
        )
