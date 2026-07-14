from rest_framework import serializers

from .models import Order


class OrderSummaryBrokenSerializer(serializers.ModelSerializer):
    """Demonstrates the regression: each field below triggers its own query
    per row because the queryset it's fed has no select_related/prefetch_related
    and no DB-side aggregation. See orders/views.py::OrderSummaryBrokenView.
    """

    customer_name = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer_name", "item_count", "total"]

    def get_customer_name(self, obj):
        # obj.customer is a lazy FK access -> 1 query per order unless
        # select_related('customer') was used upstream.
        return obj.customer.name

    def get_item_count(self, obj):
        # obj.items.all() is a lazy reverse-FK query -> another query per order.
        return obj.items.count()

    def get_total(self, obj):
        # Iterating obj.items.all() again re-triggers (or re-uses an
        # unprefetched) queryset -> yet another query per order.
        return sum(item.quantity * item.unit_price for item in obj.items.all())


class OrderSummarySerializer(serializers.ModelSerializer):
    """Fixed version: item_count/total are DB-computed annotations already
    present on the queryset (see OrderSummaryView), so accessing them here
    reads from Python memory, not the database.
    """

    customer_name = serializers.CharField(source="customer.name")
    item_count = serializers.IntegerField()
    total = serializers.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer_name", "item_count", "total"]
