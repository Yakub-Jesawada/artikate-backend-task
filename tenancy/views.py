from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order


class TenantOrderListView(APIView):
    """Returns orders for whatever tenant TenantMiddleware resolved for this
    request. Deliberately does not accept a tenant_id query param -- that
    would defeat the point of automatic scoping.
    """

    def get(self, request):
        orders = Order.objects.all()
        return Response(
            [
                {"id": o.id, "reference": o.reference, "amount": str(o.amount)}
                for o in orders
            ]
        )
