from django.contrib import admin

from .models import Document, ExtractionField, ExtractionKeyword, ExtractionProfile, FilterPreset

KEYWORD_PREFIX = "keyword:"


def _remove_field_keys(keys, owner_id=None):
    if not keys:
        return
    profile_qs = ExtractionProfile.objects.all()
    doc_qs = Document.objects.all()
    if owner_id is not None:
        profile_qs = profile_qs.filter(owner_id=owner_id)
        doc_qs = doc_qs.filter(owner_id=owner_id)

    keyword_ids = []
    if owner_id is None:
        keyword_ids = list(
            ExtractionKeyword.objects.filter(field_key__in=keys).values_list("id", flat=True)
        )
    keyword_keys = {f"{KEYWORD_PREFIX}{keyword_id}" for keyword_id in keyword_ids}
    remove_keys = set(keys) | keyword_keys

    for profile in profile_qs.iterator():
        current = profile.enabled_fields or []
        updated = [value for value in current if value not in remove_keys]
        if updated != current:
            profile.enabled_fields = updated
            profile.save(update_fields=("enabled_fields", "updated_at"))

    for doc in doc_qs.iterator():
        current = doc.selected_fields or []
        updated = [value for value in current if value not in remove_keys]
        if updated != current:
            doc.selected_fields = updated
            doc.save(update_fields=("selected_fields",))

    if owner_id is None:
        ExtractionKeyword.objects.filter(field_key__in=keys).delete()


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("id", "original_filename", "owner", "status", "uploaded_at", "processed_at")
    list_filter = ("status", "uploaded_at")
    search_fields = ("original_filename", "id", "owner__username")


@admin.register(ExtractionProfile)
class ExtractionProfileAdmin(admin.ModelAdmin):
    list_display = ("owner", "updated_at")
    search_fields = ("owner__username", "owner__email")


@admin.register(ExtractionField)
class ExtractionFieldAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "created_at")
    search_fields = ("label", "key")

    def delete_model(self, request, obj):
        _remove_field_keys({obj.key})
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        keys = set(queryset.values_list("key", flat=True))
        _remove_field_keys(keys)
        super().delete_queryset(request, queryset)


@admin.register(ExtractionKeyword)
class ExtractionKeywordAdmin(admin.ModelAdmin):
    list_display = (
        "label",
        "resolved_kind",
        "field_key",
        "inferred_type",
        "value_type",
        "strategy",
        "owner",
        "created_at",
    )
    list_filter = ("created_at", "value_type", "strategy", "resolved_kind")
    search_fields = (
        "label",
        "field_key",
        "resolved_kind",
        "inferred_type",
        "value_type",
        "strategy",
        "normalized_label",
        "owner__username",
        "owner__email",
    )

    def delete_model(self, request, obj):
        _remove_field_keys({f"{KEYWORD_PREFIX}{obj.id}"}, owner_id=obj.owner_id)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        keys_by_owner = {}
        for keyword_id, owner_id in queryset.values_list("id", "owner_id"):
            keys_by_owner.setdefault(owner_id, set()).add(f"{KEYWORD_PREFIX}{keyword_id}")
        for owner_id, keys in keys_by_owner.items():
            _remove_field_keys(keys, owner_id=owner_id)
        super().delete_queryset(request, queryset)


@admin.register(FilterPreset)
class FilterPresetAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "updated_at")
    list_filter = ("updated_at",)
    search_fields = ("name", "owner__username", "owner__email")
