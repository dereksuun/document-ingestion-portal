from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .api import (
    CsrfView,
    DocumentViewSet,
    ExtractionSettingsView,
    FilterPresetViewSet,
    HealthView,
    KeywordCreateView,
    KeywordDetailView,
    LogoutView,
    MeView,
)

router = DefaultRouter()
router.register("documents", DocumentViewSet, basename="api-documents")
router.register("presets", FilterPresetViewSet, basename="api-presets")

urlpatterns = [
    path("health/", HealthView.as_view(), name="api-health"),
    path("csrf/", CsrfView.as_view(), name="api-csrf"),
    path("auth/token/", TokenObtainPairView.as_view(), name="api-token"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="api-token-refresh"),
    path("auth/me/", MeView.as_view(), name="api-auth-me"),
    path("me/", MeView.as_view(), name="api-me"),
    path("logout/", LogoutView.as_view(), name="api-logout"),
    path("extraction-settings/", ExtractionSettingsView.as_view(), name="api-extraction-settings"),
    path("keywords/", KeywordCreateView.as_view(), name="api-keywords"),
    path("keywords/<int:keyword_id>/", KeywordDetailView.as_view(), name="api-keyword-detail"),
    path("", include(router.urls)),
]
