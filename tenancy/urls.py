from django.urls import path

from .views import TenantOrderListView

app_name = "tenancy"

urlpatterns = [
    path("orders/", TenantOrderListView.as_view(), name="orders"),
]
