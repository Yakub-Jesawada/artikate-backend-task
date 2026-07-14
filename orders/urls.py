from django.urls import path

from .views import OrderSummaryBrokenView, OrderSummaryView

app_name = "orders"

urlpatterns = [
    path("summary/", OrderSummaryView.as_view(), name="summary"),
    path("summary/broken/", OrderSummaryBrokenView.as_view(), name="summary-broken"),
]
