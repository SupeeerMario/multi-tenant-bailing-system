from django.urls import path
from . import views

urlpatterns = [
    path('tenants/', views.TenantCreateView.as_view(), name = 'create_tenant'),
    path('plans/', views.PlanCreateView.as_view(), name = 'create_plan'),
    path('subscriptions/', views.SubscriptionsCreateView.as_view(), name = 'create_subscription'),
    path('usage/', views.UsageEventListCreateView.as_view(), name = 'usage'),
]