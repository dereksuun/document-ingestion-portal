import json
import os
import re

from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.utils.text import get_valid_filename
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .forms import MAX_FILE_SIZE_BYTES, MAX_FILE_SIZE_MB
from .models import (
    Document,
    DocumentStatus,
    ExtractionField,
    ExtractionKeyword,
    ExtractionProfile,
    FilterPreset,
)
from .services import KEYWORD_PREFIX, _normalize_for_match, sanitize_payload
from .tasks import process_document_task

TERM_SPLIT_RE = re.compile(r"[,\s]+")
MAX_BULK = 25


class IsAuthenticatedOrOptions(IsAuthenticated):
    def has_permission(self, request, view):
        if request.method == "OPTIONS":
            return True
        return super().has_permission(request, view)


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


def _apply_preset_filters(
    queryset,
    preset: FilterPreset,
    *,
    experience_min_years: int | None = None,
    age_min_years: int | None = None,
    age_max_years: int | None = None,
    exclude_unknowns: bool | None = None,
):
    from django.db.models import Q

    if not preset:
        return queryset
    if experience_min_years is None:
        experience_min_years = preset.experience_min_years
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


class HealthView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"ok": True})


class DocumentSerializer(serializers.ModelSerializer):
    filename = serializers.CharField(source="original_filename", read_only=True)
    created_at = serializers.DateTimeField(source="uploaded_at", read_only=True)
    updated_at = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            "id",
            "filename",
            "status",
            "extracted_age_years",
            "extracted_experience_years",
            "error_message",
            "created_at",
            "updated_at",
        ]

    def get_updated_at(self, obj):
        return obj.processed_at or obj.uploaded_at


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
        age_min_raw = (self.request.query_params.get("age_min_years") or "").strip()
        age_max_raw = (self.request.query_params.get("age_max_years") or "").strip()
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

        exp_min_override = _parse_int(exp_min_raw)
        age_min_override = _parse_int(age_min_raw)
        age_max_override = _parse_int(age_max_raw)

        effective_terms = search_terms
        effective_mode = mode
        if preset_id:
            try:
                preset = FilterPreset.objects.get(id=preset_id, owner=user)
            except FilterPreset.DoesNotExist as exc:
                raise NotFound("Preset not found.") from exc

            qs = _apply_preset_filters(
                qs,
                preset,
                experience_min_years=exp_min_override,
                age_min_years=age_min_override,
                age_max_years=age_max_override,
                exclude_unknowns=preset.exclude_unknowns,
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

        qs = _apply_term_filters(qs, effective_terms, mode=effective_mode)
        if exclude_terms:
            for term in exclude_terms:
                qs = qs.exclude(text_content_norm__icontains=term)

        return qs.order_by("-uploaded_at")

    def list(self, request, *args, **kwargs):
        queryset = self._apply_filters(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
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
        if selected_fields is None:
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


class FilterPresetViewSet(viewsets.ModelViewSet):
    serializer_class = FilterPresetSerializer
    permission_classes = [IsAuthenticatedOrOptions]

    def get_queryset(self):
        return FilterPreset.objects.filter(owner=self.request.user).order_by("name")

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
