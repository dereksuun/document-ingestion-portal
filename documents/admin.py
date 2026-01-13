from django.contrib import admin

from .models import Document, ExtractionKeyword, ExtractionProfile


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "owner", "status", "uploaded_at", "processed_at")
    list_filter = ("status", "uploaded_at")
    search_fields = ("original_filename", "id", "owner__username")


@admin.register(ExtractionProfile)
class ExtractionProfileAdmin(admin.ModelAdmin):
    list_display = ("owner", "updated_at")
    search_fields = ("owner__username", "owner__email")


@admin.register(ExtractionKeyword)
class ExtractionKeywordAdmin(admin.ModelAdmin):
    list_display = ("label", "owner", "created_at")
    list_filter = ("created_at",)
    search_fields = ("label", "normalized_label", "owner__username", "owner__email")
