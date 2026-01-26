from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .api import DocumentViewSet, FilterPresetViewSet, HealthView

router = DefaultRouter()
router.register("documents", DocumentViewSet, basename="api-documents")
router.register("presets", FilterPresetViewSet, basename="api-presets")

urlpatterns = [
    path("health/", HealthView.as_view(), name="api-health"),
    path("", include(router.urls)),
]
