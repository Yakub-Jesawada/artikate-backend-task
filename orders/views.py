from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Sum
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Customer, Order
from .serializers import OrderSummaryBrokenSerializer, OrderSummarySerializer


class OrderSummaryBrokenView(APIView):
    """Reproduces the incident: /api/orders/summary/broken/?customer_id=<id>

    Root cause (see ANSWERS.md Section 1): no select_related/prefetch_related
    on the base queryset, plus per-row aggregation done in Python via
    SerializerMethodField, so N orders costs roughly 1 + 2N queries.
    """

    def get(self, request):
        customer_id = request.query_params.get("customer_id")
        customer = get_object_or_404(Customer, pk=customer_id)

        orders = Order.objects.filter(customer=customer)  # no select_related/prefetch_related
        serializer = OrderSummaryBrokenSerializer(orders, many=True)
        return Response(serializer.data)


class OrderSummaryView(APIView):
    """Fixed endpoint: /api/orders/summary/?customer_id=<id>

    - select_related('customer') folds the FK lookup into the same SQL join,
      eliminating the per-row customer query.
    - annotate(item_count=Count(...), total=Sum(...)) pushes the aggregation
      into the database (GROUP BY), eliminating the per-row items query and
      the per-row Python summation.

    Net effect: a fixed, small number of queries regardless of order count.
    """

    def get(self, request):
        customer_id = request.query_params.get("customer_id")
        customer = get_object_or_404(Customer, pk=customer_id)

        line_total = ExpressionWrapper(
            F("items__quantity") * F("items__unit_price"),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        )

        orders = (
            Order.objects.filter(customer=customer)
            .select_related("customer")
            .annotate(
                item_count=Count("items", distinct=True),
                total=Sum(line_total),
            )
            .order_by("-created_at")
        )
        for order in orders:
            if order.total is None:
                order.total = Decimal("0.00")

        serializer = OrderSummarySerializer(orders, many=True)
        return Response(serializer.data)
