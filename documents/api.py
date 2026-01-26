import io
import json
import os
import re
import zipfile

from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.text import get_valid_filename
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .forms import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB
from .intent import resolve_intent
from .intent_catalog import TYPE_BY_BUILTIN
from .models import (
    Document,
    DocumentStatus,
    ExtractionField,
    ExtractionKeyword,
    ExtractionProfile,
    FilterPreset,
    STRATEGY_CHOICES,
    VALUE_TYPE_CHOICES,
    _normalize_keyword,
)
from .services import CORE_FIELD_KEYS, KEYWORD_PREFIX, _normalize_for_match, sanitize_payload
from .tasks import process_document_task

TERM_SPLIT_RE = re.compile(r"[,\s]+")
MAX_BULK = 25
SEARCH_SNIPPET_LEN = 120


class IsAuthenticatedOrOptions(IsAuthenticated):
    def has_permission(self, request, view):
        if request.method == "OPTIONS":
            return True
        return super().has_permission(request, view)


class IsAdminOrOptions(IsAdminUser):
    def has_permission(self, request, view):
        if request.method == "OPTIONS":
            return True
        return super().has_permission(request, view)


class DocumentPagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 100


def _split_terms(raw: str) -> list[str]:
    if not raw:
        return []
    if ";" in raw:
        parts = [term.strip() for term in raw.split(";") if term.strip()]
    else:
        parts = [term.strip() for term in TERM_SPLIT_RE.split(raw) if term.strip()]
    normalized_terms = []
    seen = set()
    for term in parts:
        normalized = _normalize_for_match(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_terms.append(normalized)
    return normalized_terms


def _apply_term_filters(queryset, terms: list[str], *, mode: str = "all", field: str = "text_content_norm"):
    if not terms:
        return queryset
    if mode == "any":
        from django.db.models import Q

        query = Q()
        for term in terms:
            query |= Q(**{f"{field}__icontains": term})
        return queryset.filter(query)
    for term in terms:
        queryset = queryset.filter(**{f"{field}__icontains": term})
    return queryset


def _build_snippet(text: str, terms: list[str], max_len: int = SEARCH_SNIPPET_LEN) -> str:
    if not text or not terms:
        return ""
    normalized = " ".join(text.split())
    lowered = _normalize_for_match(normalized)
    match_index = None
    match_term = ""
    for term in terms:
        idx = lowered.find(term)
        if idx == -1:
            continue
        if match_index is None or idx < match_index:
            match_index = idx
            match_term = term
    if match_index is None:
        return ""
    radius = max_len // 2
    start = max(0, match_index - radius)
    end = min(len(normalized), match_index + len(match_term) + radius)
    snippet = normalized[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(normalized):
        snippet = snippet + "..."
    return snippet


def _apply_preset_filters(
    queryset,
    preset: FilterPreset,
    *,
    experience_min_years: int | None = None,
    experience_max_years: int | None = None,
    age_min_years: int | None = None,
    age_max_years: int | None = None,
    exclude_unknowns: bool | None = None,
):
    from django.db.models import Q

    if not preset:
        return queryset
    if experience_min_years is None:
        experience_min_years = preset.experience_min_years
    if experience_max_years is None:
        experience_max_years = preset.experience_max_years
    if age_min_years is None:
        age_min_years = preset.age_min_years
    if age_max_years is None:
        age_max_years = preset.age_max_years
    if exclude_unknowns is None:
        exclude_unknowns = preset.exclude_unknowns
    if experience_min_years is not None:
        if exclude_unknowns:
            queryset = queryset.filter(extracted_experience_years__gte=experience_min_years)
        else:
            queryset = queryset.filter(
                Q(extracted_experience_years__isnull=True)
                | Q(extracted_experience_years__gte=experience_min_years)
            )
    if experience_max_years is not None:
        if exclude_unknowns:
            queryset = queryset.filter(extracted_experience_years__lte=experience_max_years)
        else:
            queryset = queryset.filter(
                Q(extracted_experience_years__isnull=True)
                | Q(extracted_experience_years__lte=experience_max_years)
            )
    if age_min_years is not None:
        if exclude_unknowns:
            queryset = queryset.filter(extracted_age_years__gte=age_min_years)
        else:
            queryset = queryset.filter(
                Q(extracted_age_years__isnull=True) | Q(extracted_age_years__gte=age_min_years)
            )
    if age_max_years is not None:
        if exclude_unknowns:
            queryset = queryset.filter(extracted_age_years__lte=age_max_years)
        else:
            queryset = queryset.filter(
                Q(extracted_age_years__isnull=True) | Q(extracted_age_years__lte=age_max_years)
            )
    return queryset


def _build_field_choices(user):
    fields = ExtractionField.objects.order_by("label")
    field_choices = [(field.key, field.label) for field in fields]
    keywords = ExtractionKeyword.objects.filter(owner=user).order_by("label")
    keyword_choices = [(f"{KEYWORD_PREFIX}{keyword.id}", keyword.label) for keyword in keywords]
    return field_choices + keyword_choices


def _filter_enabled_fields(choices, enabled_fields):
    allowed = {value for value, _ in choices}
    filtered = [value for value in (enabled_fields or []) if value in allowed]
    return list(dict.fromkeys(filtered))


def _get_profile(user):
    profile, _ = ExtractionProfile.objects.get_or_create(owner=user)
    if profile.enabled_fields is None:
        profile.enabled_fields = []
        profile.save(update_fields=["enabled_fields"])
    return profile


def _build_json_filename(doc):
    base_name = doc.original_filename or str(doc.id)
    base_name = os.path.splitext(base_name)[0]
    safe_name = get_valid_filename(base_name) or str(doc.id)
    return f"{safe_name}.json"


def _safe_name(filename: str, fallback: str) -> str:
    base_name = os.path.basename(filename or "").strip()
    if not base_name:
        base_name = fallback
    safe_name = get_valid_filename(base_name)
    return safe_name or fallback


def _unique_name(filename: str, used_names: set[str], token: str) -> str:
    if filename not in used_names:
        used_names.add(filename)
        return filename
    base, ext = os.path.splitext(filename)
    candidate = f"{base}-{token}{ext}"
    if candidate in used_names:
        counter = 2
        while f"{base}-{token}-{counter}{ext}" in used_names:
            counter += 1
        candidate = f"{base}-{token}-{counter}{ext}"
    used_names.add(candidate)
    return candidate


def _iter_file_chunks(file_obj, chunk_size=1024 * 1024):
    if hasattr(file_obj, "chunks"):
        for chunk in file_obj.chunks(chunk_size=chunk_size):
            if chunk:
                yield chunk
        return
    while True:
        chunk = file_obj.read(chunk_size)
        if not chunk:
            break
        yield chunk


def _field_to_dict(field, enabled_fields):
    is_core = field.key in CORE_FIELD_KEYS
    return {
        "key": field.key,
        "label": field.label,
        "group": "core" if is_core else "builtin",
        "enabled_by_default": is_core,
        "is_active": field.key in enabled_fields,
        "value_type": TYPE_BY_BUILTIN.get(field.key, ""),
    }


def _keyword_to_dict(keyword, enabled_fields):
    keyword_key = f"{KEYWORD_PREFIX}{keyword.id}"
    return {
        "id": keyword.id,
        "keyword_key": keyword_key,
        "label": keyword.label,
        "field_key": keyword.field_key or "",
        "match_strategy": keyword.match_strategy or "",
        "matcher": keyword.strategy or "",
        "is_active": keyword_key in enabled_fields,
        "value_type": keyword.value_type,
        "inferred_type": keyword.inferred_type,
        "resolved_kind": keyword.resolved_kind,
        "strategy": keyword.strategy,
        "strategy_params": keyword.strategy_params or {},
        "anchors": keyword.anchors or [],
        "confidence": keyword.confidence,
    }


def _build_extraction_settings_payload(user, enabled_fields=None):
    profile = _get_profile(user)
    choices = _build_field_choices(user)
    if enabled_fields is None:
        enabled_fields = _filter_enabled_fields(choices, profile.enabled_fields)
        if enabled_fields != (profile.enabled_fields or []):
            profile.enabled_fields = enabled_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])

    fields = ExtractionField.objects.order_by("label")
    available_fields = [_field_to_dict(field, enabled_fields) for field in fields]
    keywords = ExtractionKeyword.objects.filter(owner=user).order_by("label")
    keyword_items = [_keyword_to_dict(keyword, enabled_fields) for keyword in keywords]

    return {
        "enabled_fields": enabled_fields,
        "available_fields": available_fields,
        "keywords": keyword_items,
    }


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"ok": True})


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"ok": True})


class MeView(APIView):
    permission_classes = [IsAuthenticatedOrOptions]

    def get(self, request):
        user = request.user
        return Response(
            {
                "id": user.id,
                "username": user.get_username(),
                "email": user.email or "",
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
            }
        )


class LogoutView(APIView):
    permission_classes = [IsAuthenticatedOrOptions]

    def post(self, request):
        from django.contrib.auth import logout

        logout(request)
        return Response({"ok": True})


class ExtractionSettingsView(APIView):
    permission_classes = [IsAuthenticatedOrOptions]

    def get(self, request):
        payload = _build_extraction_settings_payload(request.user)
        return Response(payload)

    def put(self, request):
        enabled_fields = request.data.get("enabled_fields")
        if not isinstance(enabled_fields, list):
            raise ValidationError({"enabled_fields": "enabled_fields must be a list."})

        profile = _get_profile(request.user)
        choices = _build_field_choices(request.user)
        enabled_fields = _filter_enabled_fields(choices, enabled_fields)
        profile.enabled_fields = enabled_fields
        profile.save(update_fields=["enabled_fields", "updated_at"])

        payload = _build_extraction_settings_payload(request.user, enabled_fields=enabled_fields)
        return Response(payload)


class KeywordCreateView(APIView):
    permission_classes = [IsAdminOrOptions]

    def post(self, request):
        label = (
            request.data.get("label")
            or request.data.get("keyword")
            or request.data.get("new_keyword")
            or ""
        ).strip()
        value_type_raw = request.data.get("value_type") or ""
        strategy_raw = request.data.get("strategy") or ""
        strategy_params_raw = request.data.get("strategy_params") or {}

        normalized = _normalize_keyword(label)
        if not label:
            raise ValidationError({"label": "label is required."})
        if not normalized:
            raise ValidationError({"label": "label is invalid."})
        if ExtractionKeyword.objects.filter(owner=request.user, normalized_label=normalized).exists():
            raise ValidationError({"label": "label already exists."})

        builtin_fields = list(ExtractionField.objects.values_list("key", "label"))
        intent = resolve_intent(label, builtin_fields, allow_llm=False)
        anchors = intent.anchors or [label.strip()]
        value_types = {key for key, _ in VALUE_TYPE_CHOICES}
        strategies = {key for key, _ in STRATEGY_CHOICES}

        inferred_value_type = (intent.inferred_type or "text").lower()
        if inferred_value_type == "postal":
            inferred_value_type = "address"
        value_type = (value_type_raw or inferred_value_type).lower()
        if value_type not in value_types:
            value_type = inferred_value_type if inferred_value_type in value_types else "text"

        if strategy_raw:
            strategy = strategy_raw.lower()
        else:
            strategy = "below_n_lines" if value_type == "block" else "after_label"
        if strategy not in strategies:
            strategy = "after_label"

        strategy_params = {}
        if isinstance(strategy_params_raw, str):
            try:
                strategy_params = json.loads(strategy_params_raw) if strategy_params_raw else {}
            except json.JSONDecodeError:
                strategy_params = {}
        elif isinstance(strategy_params_raw, dict):
            strategy_params = strategy_params_raw
        if not isinstance(strategy_params, dict):
            strategy_params = {}
        if strategy == "below_n_lines" and "max_lines" not in strategy_params:
            strategy_params["max_lines"] = 3

        keyword = ExtractionKeyword.objects.create(
            owner=request.user,
            label=label,
            field_key=intent.builtin_key if intent.kind == "builtin" else "",
            resolved_kind=intent.kind,
            inferred_type=value_type,
            value_type=value_type,
            strategy=strategy,
            strategy_params=strategy_params,
            anchors=anchors,
            match_strategy=intent.match_strategy,
            confidence=float(intent.confidence or 0.0),
        )

        profile = _get_profile(request.user)
        choices = _build_field_choices(request.user)
        enabled_fields = _filter_enabled_fields(choices, profile.enabled_fields)
        keyword_key = f"{KEYWORD_PREFIX}{keyword.id}"
        if keyword_key not in enabled_fields:
            enabled_fields.append(keyword_key)
            profile.enabled_fields = enabled_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])

        payload = _keyword_to_dict(keyword, enabled_fields)
        return Response(payload, status=status.HTTP_201_CREATED)


class KeywordDetailView(APIView):
    permission_classes = [IsAdminOrOptions]

    def delete(self, request, keyword_id):
        keyword = get_object_or_404(ExtractionKeyword, id=keyword_id, owner=request.user)
        keyword_key = f"{KEYWORD_PREFIX}{keyword.id}"

        profile = _get_profile(request.user)
        if keyword_key in (profile.enabled_fields or []):
            profile.enabled_fields = [value for value in profile.enabled_fields if value != keyword_key]
            profile.save(update_fields=["enabled_fields", "updated_at"])

        for doc in Document.objects.filter(owner=request.user).iterator():
            selected = doc.selected_fields or []
            if keyword_key not in selected:
                continue
            doc.selected_fields = [value for value in selected if value != keyword_key]
            doc.save(update_fields=["selected_fields"])

        keyword.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DocumentSerializer(serializers.ModelSerializer):
    filename = serializers.CharField(source="original_filename", read_only=True)
    created_at = serializers.DateTimeField(source="uploaded_at", read_only=True)
    updated_at = serializers.SerializerMethodField()
    search_snippet = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "filename",
            "status",
            "extracted_age_years",
            "extracted_experience_years",
            "error_message",
            "search_snippet",
            "created_at",
            "updated_at",
        ]

    def get_updated_at(self, obj):
        return obj.processed_at or obj.uploaded_at

    def get_search_snippet(self, obj):
        terms = self.context.get("snippet_terms") or []
        if not terms:
            return ""
        source = obj.text_content or obj.extracted_text or ""
        return _build_snippet(source, terms)


class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    selected_fields = serializers.ListField(child=serializers.CharField(), required=False)

    def validate_file(self, value):
        name = (getattr(value, "name", "") or "").lower()
        content_type = (getattr(value, "content_type", "") or "").lower()
        is_pdf = name.endswith(".pdf") or content_type == "application/pdf"
        if not is_pdf:
            raise serializers.ValidationError("Only PDF files are allowed.")
        if getattr(value, "size", 0) > MAX_FILE_SIZE_BYTES:
            raise serializers.ValidationError(f"File exceeds {MAX_FILE_SIZE_MB} MB.")
        return value

    def validate_selected_fields(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("selected_fields must be a list.")
        return value


class FilterPresetSerializer(serializers.ModelSerializer):
    class Meta:
        model = FilterPreset
        fields = [
            "id",
            "name",
            "keywords",
            "exclude_terms_text",
            "keywords_mode",
            "exclude_unknowns",
            "experience_min_years",
            "experience_max_years",
            "age_min_years",
            "age_max_years",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_keywords(self, value):
        if value is None:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("keywords must be a list.")
        normalized_terms = []
        seen = set()
        for term in value:
            if not isinstance(term, str):
                continue
            normalized = _normalize_for_match(term)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            normalized_terms.append(normalized)
        return normalized_terms

    def validate_exclude_terms_text(self, value):
        if value is None:
            return ""
        if isinstance(value, list):
            parts = [str(term).strip() for term in value if str(term).strip()]
            return "; ".join(parts)
        if not isinstance(value, str):
            raise serializers.ValidationError("exclude_terms_text must be a string.")
        return value.strip()

    def validate(self, attrs):
        age_min = attrs.get("age_min_years")
        age_max = attrs.get("age_max_years")
        exp_min = attrs.get("experience_min_years")
        exp_max = attrs.get("experience_max_years")
        if age_min is not None and age_max is not None and age_min > age_max:
            raise serializers.ValidationError({"age_max_years": "age_max_years must be >= age_min_years."})
        if exp_min is not None and exp_max is not None and exp_min > exp_max:
            raise serializers.ValidationError(
                {"experience_max_years": "experience_max_years must be >= experience_min_years."}
            )
        return attrs


class DocumentViewSet(viewsets.GenericViewSet):
    queryset = Document.objects.all()
    permission_classes = [IsAuthenticatedOrOptions]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_serializer_class(self):
        if self.action == "create":
            return DocumentUploadSerializer
        return DocumentSerializer

    def get_queryset(self):
        return Document.objects.filter(owner=self.request.user)

    def _apply_filters(self, qs):
        user = self.request.user
        status_param = (self.request.query_params.get("status") or "").strip()
        if status_param:
            statuses = [value.strip().upper() for value in status_param.split(",") if value.strip()]
            if statuses:
                qs = qs.filter(status__in=statuses)

        search_query = (self.request.query_params.get("q") or "").strip()
        exclude_query = (self.request.query_params.get("exclude") or "").strip()
        preset_id = (self.request.query_params.get("preset") or "").strip()
        exp_min_raw = (self.request.query_params.get("experience_min_years") or "").strip()
        exp_max_raw = (self.request.query_params.get("experience_max_years") or "").strip()
        age_min_raw = (self.request.query_params.get("age_min_years") or "").strip()
        age_max_raw = (self.request.query_params.get("age_max_years") or "").strip()
        exclude_unknowns_raw = (self.request.query_params.get("exclude_unknowns") or "").strip()
        mode = (self.request.query_params.get("mode") or "all").lower()
        if mode not in {"all", "any"}:
            mode = "all"

        search_terms = _split_terms(search_query)
        exclude_terms = _split_terms(exclude_query)

        def _parse_int(value: str) -> int | None:
            if not value:
                return None
            try:
                return int(value)
            except ValueError:
                return None

        def _parse_bool(value: str) -> bool | None:
            lowered = (value or "").strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
            return None

        exp_min_override = _parse_int(exp_min_raw)
        exp_max_override = _parse_int(exp_max_raw)
        age_min_override = _parse_int(age_min_raw)
        age_max_override = _parse_int(age_max_raw)
        exclude_unknowns_override = _parse_bool(exclude_unknowns_raw)

        effective_terms = search_terms
        effective_mode = mode
        effective_exclude_terms = exclude_terms
        if preset_id:
            try:
                preset = FilterPreset.objects.get(id=preset_id, owner=user)
            except FilterPreset.DoesNotExist as exc:
                raise NotFound("Preset not found.") from exc

            qs = _apply_preset_filters(
                qs,
                preset,
                experience_min_years=exp_min_override,
                experience_max_years=exp_max_override,
                age_min_years=age_min_override,
                age_max_years=age_max_override,
                exclude_unknowns=exclude_unknowns_override
                if exclude_unknowns_override is not None
                else preset.exclude_unknowns,
            )
            if preset.keywords:
                if not search_terms:
                    effective_terms = preset.keywords
                    preset_mode = (preset.keywords_mode or "all").lower()
                    if preset_mode in {"all", "any"}:
                        effective_mode = preset_mode
                elif search_terms == preset.keywords:
                    preset_mode = (preset.keywords_mode or "all").lower()
                    if preset_mode in {"all", "any"}:
                        effective_mode = preset_mode
            preset_exclude_terms = _split_terms(preset.exclude_terms_text)
            if preset_exclude_terms and not exclude_terms:
                effective_exclude_terms = preset_exclude_terms

        qs = _apply_term_filters(qs, effective_terms, mode=effective_mode)
        if effective_exclude_terms:
            for term in effective_exclude_terms:
                qs = qs.exclude(text_content_norm__icontains=term)

        return qs.order_by("-uploaded_at"), effective_terms

    def list(self, request, *args, **kwargs):
        queryset, snippet_terms = self._apply_filters(self.get_queryset())
        paginator = DocumentPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        context = {**self.get_serializer_context(), "snippet_terms": snippet_terms}
        if page is not None:
            serializer = self.get_serializer(page, many=True, context=context)
            return paginator.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True, context=context)
        return Response(serializer.data)

    def retrieve(self, request, *args, **kwargs):
        doc = self.get_object()
        serializer = self.get_serializer(doc)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = DocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file_obj = serializer.validated_data["file"]
        selected_fields = serializer.validated_data.get("selected_fields")

        profile = _get_profile(request.user)
        choices = _build_field_choices(request.user)
        if not selected_fields:
            selected_fields = _filter_enabled_fields(choices, profile.enabled_fields)
            if selected_fields != (profile.enabled_fields or []):
                profile.enabled_fields = selected_fields
                profile.save(update_fields=["enabled_fields", "updated_at"])
        else:
            selected_fields = _filter_enabled_fields(choices, selected_fields)

        with transaction.atomic():
            doc = Document.objects.create(
                owner=request.user,
                file=file_obj,
                original_filename=file_obj.name,
                selected_fields=selected_fields,
            )
            transaction.on_commit(lambda doc_id=str(doc.id): process_document_task.delay(doc_id))

        return Response({"id": str(doc.id), "status": doc.status}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["get"], url_path="enabled-fields")
    def enabled_fields(self, request):
        profile = _get_profile(request.user)
        choices = _build_field_choices(request.user)
        selected_fields = _filter_enabled_fields(choices, profile.enabled_fields)

        if selected_fields != (profile.enabled_fields or []):
            profile.enabled_fields = selected_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])

        return Response({"enabled_fields": selected_fields})

    @action(detail=True, methods=["post"], url_path="reprocess")
    def reprocess(self, request, pk=None):
        doc = self.get_object()
        force_ocr_raw = request.data.get("force_ocr", request.query_params.get("force_ocr", ""))
        force_ocr = str(force_ocr_raw).lower() in {"1", "true", "yes"}

        if doc.status == DocumentStatus.PROCESSING:
            return Response({"id": str(doc.id), "status": doc.status}, status=status.HTTP_202_ACCEPTED)

        transaction.on_commit(
            lambda doc_id=str(doc.id): process_document_task.delay(
                doc_id,
                force=True,
                force_ocr=force_ocr,
            )
        )
        return Response({"id": str(doc.id), "status": doc.status}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=["post"], url_path="bulk-reprocess")
    def bulk_reprocess(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "ids must be a non-empty list."})

        ids = list(dict.fromkeys(ids))
        ids = ids[:MAX_BULK]

        docs = list(
            Document.objects.filter(owner=request.user, id__in=ids)
            .exclude(status=DocumentStatus.PROCESSING)
        )

        for doc in docs:
            transaction.on_commit(
                lambda doc_id=str(doc.id): process_document_task.delay(
                    doc_id,
                    force=True,
                    force_ocr=False,
                )
            )

        return Response({"queued": len(docs)}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"], url_path="download-json")
    def download_json(self, request, pk=None):
        doc = self.get_object()
        json_data = sanitize_payload(doc.extracted_json or {})
        payload = json.dumps(json_data, ensure_ascii=False, indent=2)
        filename = _build_json_filename(doc)
        response = HttpResponse(payload, content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @action(detail=True, methods=["get"], url_path="download-file")
    def download_file(self, request, pk=None):
        doc = self.get_object()
        filename = doc.original_filename or os.path.basename(doc.file.name) or str(doc.id)
        try:
            return FileResponse(doc.file.open("rb"), as_attachment=True, filename=filename)
        except (FileNotFoundError, ValueError) as exc:
            raise NotFound("File not found.") from exc

    @action(detail=False, methods=["post"], url_path="bulk-download-json")
    def download_json_bulk(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "ids must be a non-empty list."})

        ids = list(dict.fromkeys(ids))[:MAX_BULK]
        docs = list(
            Document.objects.filter(owner=request.user, id__in=ids)
            .order_by("-uploaded_at")
        )

        buffer = io.BytesIO()
        used_names = set()
        added = 0

        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for doc in docs:
                if not doc.extracted_json:
                    continue
                json_data = sanitize_payload(doc.extracted_json or {})
                payload = json.dumps(json_data, ensure_ascii=False, indent=2)
                filename = _build_json_filename(doc)
                if filename in used_names:
                    base, ext = os.path.splitext(filename)
                    filename = f"{base}-{str(doc.id)[:8]}{ext}"
                used_names.add(filename)
                zip_file.writestr(filename, payload)
                added += 1

        if added == 0:
            return Response({"detail": "No JSON available for download."}, status=status.HTTP_400_BAD_REQUEST)

        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = 'attachment; filename="documentos-json.zip"'
        return response

    @action(detail=False, methods=["post"], url_path="bulk-download-files")
    def download_files_bulk(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValidationError({"ids": "ids must be a non-empty list."})

        ids = list(dict.fromkeys(ids))[:MAX_BULK]
        docs = list(
            Document.objects.filter(owner=request.user, id__in=ids)
            .order_by("-uploaded_at")
        )

        buffer = io.BytesIO()
        used_names = set()
        added = 0
        missing = 0

        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for doc in docs:
                if not doc.file or not doc.file.name:
                    missing += 1
                    continue
                original_name = doc.original_filename or os.path.basename(doc.file.name) or str(doc.id)
                safe_name = _safe_name(original_name, str(doc.id))
                filename = _unique_name(safe_name, used_names, str(doc.id)[:8])
                try:
                    with doc.file.open("rb") as file_obj:
                        with zip_file.open(filename, "w") as dest:
                            for chunk in _iter_file_chunks(file_obj):
                                dest.write(chunk)
                    added += 1
                except Exception:
                    missing += 1

        if added == 0:
            return Response({"detail": "No files available for download."}, status=status.HTTP_400_BAD_REQUEST)

        buffer.seek(0)
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = 'attachment; filename="documentos-arquivos.zip"'
        return response


class FilterPresetViewSet(viewsets.ModelViewSet):
    serializer_class = FilterPresetSerializer
    permission_classes = [IsAuthenticatedOrOptions]

    def get_queryset(self):
        return FilterPreset.objects.filter(owner=self.request.user).order_by("name")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
