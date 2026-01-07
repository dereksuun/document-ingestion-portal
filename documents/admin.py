from django.contrib import admin

from .models import Document


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "owner", "status", "uploaded_at", "processed_at")
    list_filter = ("status", "uploaded_at")
    search_fields = ("original_filename", "id", "owner__username")
