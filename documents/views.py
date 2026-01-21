import io
import json
import logging
import os
import re
import zipfile

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import get_valid_filename

from .forms import ExtractionSettingsForm, FilterPresetForm, KeywordForm, MultiUploadForm
from .intent import resolve_intent
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
from .services import (
    KEYWORD_PREFIX,
    _normalize_for_match,
    extract_age_years,
    extract_contact_phone,
    extract_experience_years,
    process_document,
    sanitize_payload,
)

PAGE_SIZE = 10
MAX_BULK = 25
SEARCH_SNIPPET_LEN = 120

TERM_SPLIT_RE = re.compile(r"[,\s]+")

logger = logging.getLogger(__name__)


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
        query = Q()
        for term in terms:
            query |= Q(**{f"{field}__icontains": term})
        return queryset.filter(query)
    for term in terms:
        queryset = queryset.filter(**{f"{field}__icontains": term})
    return queryset


def _apply_preset_filters(queryset, preset: FilterPreset):
    if not preset:
        return queryset
    if preset.document_type:
        queryset = queryset.filter(document_type=preset.document_type)
    mode = (preset.keywords_mode or "all").lower()
    if mode not in {"all", "any"}:
        mode = "all"
    keywords = preset.keywords or []
    queryset = _apply_term_filters(queryset, keywords, mode=mode)
    if preset.experience_min_years is not None:
        queryset = queryset.filter(extracted_experience_years__gte=preset.experience_min_years)
    if preset.experience_max_years is not None:
        queryset = queryset.filter(extracted_experience_years__lte=preset.experience_max_years)
    if preset.age_min_years is not None:
        queryset = queryset.filter(extracted_age_years__gte=preset.age_min_years)
    if preset.age_max_years is not None:
        queryset = queryset.filter(extracted_age_years__lte=preset.age_max_years)
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


def _get_keyword_map(owner, selected_fields):
    keyword_ids = []
    for field in selected_fields or []:
        if not field.startswith(KEYWORD_PREFIX):
            continue
        raw_id = field.split(":", 1)[1]
        if not raw_id.isdigit():
            continue
        keyword_ids.append(int(raw_id))
    if not keyword_ids:
        return {}
    keywords = ExtractionKeyword.objects.filter(owner=owner, id__in=keyword_ids)
    mapping = {}
    for keyword in keywords:
        mapping[f"{KEYWORD_PREFIX}{keyword.id}"] = {
            "label": keyword.label,
            "resolved_kind": keyword.resolved_kind,
            "field_key": keyword.field_key,
            "inferred_type": keyword.inferred_type,
            "value_type": keyword.value_type,
            "strategy": keyword.strategy,
            "strategy_params": keyword.strategy_params or {},
            "anchors": keyword.anchors or [],
            "match_strategy": keyword.match_strategy,
            "confidence": keyword.confidence,
        }
    return mapping


def _get_profile(user):
    profile, _ = ExtractionProfile.objects.get_or_create(owner=user)
    if profile.enabled_fields is None:
        profile.enabled_fields = []
        profile.save(update_fields=["enabled_fields"])
    return profile


def _apply_extracted_fields(doc: Document, extracted_text: str, payload: dict):
    text_value = extracted_text or ""
    normalized = _normalize_for_match(text_value)
    doc.extracted_text_normalized = normalized
    doc.text_content = text_value
    doc.text_content_norm = normalized
    doc.document_type = (payload or {}).get("document_type") or ""
    doc.contact_phone = extract_contact_phone(text_value)
    doc.extracted_age_years = extract_age_years(text_value)
    doc.extracted_experience_years = extract_experience_years(text_value)


def _get_force_ocr(request) -> bool:
    return request.POST.get("force_ocr") == "1" or request.GET.get("force_ocr") == "1"


@login_required
def upload_documents(request):
    if request.method == "POST":
        form = MultiUploadForm(request.POST, request.FILES)
        if form.is_valid():
            files = form.cleaned_data["files"]
            filenames = [file_obj.name for file_obj in files]
            profile = _get_profile(request.user)
            choices = _build_field_choices(request.user)
            selected_fields = _filter_enabled_fields(choices, profile.enabled_fields)
            if selected_fields != (profile.enabled_fields or []):
                profile.enabled_fields = selected_fields
                profile.save(update_fields=["enabled_fields", "updated_at"])
            keyword_map = _get_keyword_map(request.user, selected_fields)
            created_docs = []
            logger.info(
                "upload_documents user=%s count=%s files=%s",
                request.user.id,
                len(files),
                filenames,
            )
            with transaction.atomic():
                for file_obj in files:
                    doc = Document.objects.create(
                        owner=request.user,
                        file=file_obj,
                        original_filename=file_obj.name,
                        selected_fields=selected_fields,
                    )
                    created_docs.append(doc)
            for doc in created_docs:
                logger.info("process_start doc=%s file=%s action=auto", doc.id, doc.original_filename)
                doc.mark_processing()
                doc.save(
                    update_fields=[
                        "status",
                        "processed_at",
                        "error_message",
                        "extracted_json",
                        "extracted_text",
                        "extracted_text_normalized",
                        "text_content",
                        "text_content_norm",
                        "document_type",
                        "contact_phone",
                        "extracted_age_years",
                        "extracted_experience_years",
                        "ocr_used",
                        "text_quality",
                    ]
                )
                try:
                    data, extracted_text, ocr_used, text_quality = process_document(
                        doc.file.path,
                        doc.selected_fields or [],
                        keyword_map=keyword_map,
                        doc_id=str(doc.id),
                        filename=doc.original_filename,
                    )
                    _apply_extracted_fields(doc, extracted_text, data)
                    doc.mark_done(
                        data,
                        extracted_text=extracted_text,
                        ocr_used=ocr_used,
                        text_quality=text_quality,
                    )
                    doc.save()
                    logger.info("process_done doc=%s file=%s action=auto", doc.id, doc.original_filename)
                except Exception as exc:
                    doc.mark_failed(str(exc))
                    doc.save(update_fields=["status", "processed_at", "error_message"])
                    logger.exception(
                        "process_failed doc=%s file=%s action=auto",
                        doc.id,
                        doc.original_filename,
                    )
            return redirect("documents_list")
    else:
        form = MultiUploadForm()

    return render(request, "documents/upload.html", {"form": form})


@login_required
def documents_list(request):
    search_query = request.GET.get("q", "").strip()
    exclude_query = request.GET.get("exclude", "").strip()
    preset_id = request.GET.get("preset", "").strip()
    mode = (request.GET.get("mode", "all") or "all").lower()
    if mode not in {"all", "any"}:
        mode = "all"

    search_terms = _split_terms(search_query)
    exclude_terms = _split_terms(exclude_query)

    docs = Document.objects.filter(owner=request.user)
    presets = list(FilterPreset.objects.filter(owner=request.user).order_by("name"))
    active_preset = None
    if preset_id:
        active_preset = get_object_or_404(FilterPreset, id=preset_id, owner=request.user)
        docs = _apply_preset_filters(docs, active_preset)

    docs = _apply_term_filters(docs, search_terms, mode=mode)
    if exclude_terms:
        for term in exclude_terms:
            docs = docs.exclude(text_content_norm__icontains=term)

    docs = docs.order_by("-uploaded_at")
    paginator = Paginator(docs, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    result_count = paginator.count

    if search_terms or exclude_terms:
        logger.info(
            "documents_search user=%s q=%s mode=%s exclude=%s results=%s",
            request.user.id,
            search_query,
            mode,
            exclude_query,
            result_count,
        )

    for doc in page_obj:
        snippet_source = doc.text_content or doc.extracted_text or ""
        doc.search_snippet = _build_snippet(snippet_source, search_terms)

    query_params = request.GET.copy()
    query_params.pop("page", None)
    querystring = query_params.urlencode()

    return render(
        request,
        "documents/list.html",
        {
            "page_obj": page_obj,
            "search_query": search_query,
            "exclude_query": exclude_query,
            "presets": presets,
            "active_preset": active_preset,
            "preset_id": preset_id,
            "mode": mode,
            "result_count": result_count,
            "querystring": querystring,
        },
    )


@login_required
def payments_view(request):
    return render(request, "payments.html")


@login_required
def extraction_settings(request):
    profile = _get_profile(request.user)
    choices = _build_field_choices(request.user)
    current_fields = _filter_enabled_fields(choices, profile.enabled_fields)
    if request.method != "POST":
        logger.info(
            "extraction_settings_load user=%s fields=%s",
            request.user.id,
            profile.enabled_fields,
        )
    if request.method == "POST":
        form = ExtractionSettingsForm(request.POST, choices=choices)
        keyword_form = KeywordForm(request.POST)
        action = request.POST.get("action", "save")
        post_enabled = request.POST.getlist("enabled_fields")
        logger.info(
            "extraction_settings_post user=%s action=%s enabled_fields=%s new_keyword=%s value_type=%s strategy=%s params=%s",
            request.user.id,
            action,
            post_enabled,
            request.POST.get("new_keyword"),
            request.POST.get("value_type"),
            request.POST.get("strategy"),
            request.POST.get("strategy_params"),
        )
        if action == "add_keyword" and keyword_form.is_valid():
            keyword_value = keyword_form.cleaned_data.get("new_keyword") or ""
            value_type_raw = keyword_form.cleaned_data.get("value_type") or ""
            strategy_raw = keyword_form.cleaned_data.get("strategy") or ""
            strategy_params_raw = keyword_form.cleaned_data.get("strategy_params") or ""
            normalized = _normalize_keyword(keyword_value)
            if not keyword_value:
                keyword_form.add_error("new_keyword", "Informe uma palavra-chave.")
            elif normalized in {"", None}:
                keyword_form.add_error("new_keyword", "Informe uma palavra-chave valida.")
            elif ExtractionKeyword.objects.filter(
                owner=request.user, normalized_label=normalized
            ).exists():
                keyword_form.add_error("new_keyword", "Essa palavra-chave ja existe.")
            else:
                builtin_fields = list(ExtractionField.objects.values_list("key", "label"))
                intent = resolve_intent(keyword_value, builtin_fields, allow_llm=False)
                anchors = intent.anchors or [keyword_value.strip()]
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
                try:
                    strategy_params = json.loads(strategy_params_raw) if strategy_params_raw else {}
                except json.JSONDecodeError:
                    strategy_params = {}
                if not isinstance(strategy_params, dict):
                    strategy_params = {}
                if strategy == "below_n_lines" and "max_lines" not in strategy_params:
                    strategy_params["max_lines"] = 3
                keyword = ExtractionKeyword.objects.create(
                    owner=request.user,
                    label=keyword_value,
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
                enabled_fields = _filter_enabled_fields(choices, post_enabled)
                enabled_fields.append(f"{KEYWORD_PREFIX}{keyword.id}")
                profile.enabled_fields = enabled_fields
                profile.save(update_fields=["enabled_fields", "updated_at"])
                logger.info(
                    "extraction_keyword_add user=%s keyword=%s kind=%s field_key=%s value_type=%s strategy=%s params=%s",
                    request.user.id,
                    keyword.label,
                    keyword.resolved_kind,
                    keyword.field_key,
                    keyword.value_type,
                    keyword.strategy,
                    keyword.strategy_params,
                )
                logger.info(
                    "extraction_settings_save user=%s enabled_fields=%s",
                    request.user.id,
                    enabled_fields,
                )
                return redirect("extraction_settings")

        if action != "add_keyword":
            enabled_fields = _filter_enabled_fields(choices, post_enabled)
            profile.enabled_fields = enabled_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])
            logger.info("extraction_profile_update user=%s fields=%s", request.user.id, enabled_fields)
            logger.info(
                "extraction_settings_save user=%s enabled_fields=%s",
                request.user.id,
                enabled_fields,
            )
            return redirect("extraction_settings")
    else:
        form = ExtractionSettingsForm(initial={"enabled_fields": current_fields}, choices=choices)
        keyword_form = KeywordForm()

    return render(
        request,
        "documents/settings.html",
        {
            "form": form,
            "keyword_form": keyword_form,
            "keywords": ExtractionKeyword.objects.filter(owner=request.user).order_by("label"),
            "fields": ExtractionField.objects.order_by("label"),
        },
    )


@login_required
def filter_presets(request):
    presets = list(FilterPreset.objects.filter(owner=request.user).order_by("name"))
    form = FilterPresetForm()
    if request.method == "POST":
        form = FilterPresetForm(request.POST)
        if form.is_valid():
            preset = form.save(commit=False)
            preset.owner = request.user
            preset.save()
            return redirect("filter_presets")
    return render(
        request,
        "documents/presets.html",
        {
            "form": form,
            "presets": presets,
            "preset": None,
        },
    )


@login_required
def filter_preset_edit(request, preset_id):
    preset = get_object_or_404(FilterPreset, id=preset_id, owner=request.user)
    presets = list(FilterPreset.objects.filter(owner=request.user).order_by("name"))
    if request.method == "POST":
        form = FilterPresetForm(request.POST, instance=preset)
        if form.is_valid():
            form.save()
            return redirect("filter_presets")
    else:
        form = FilterPresetForm(instance=preset)
    return render(
        request,
        "documents/presets.html",
        {
            "form": form,
            "presets": presets,
            "preset": preset,
        },
    )


@login_required
def delete_keyword(request, keyword_id):
    if not request.user.is_staff:
        return HttpResponseForbidden("Sem permissao.")
    if request.method != "POST":
        return HttpResponseForbidden("Metodo invalido.")

    keyword = get_object_or_404(ExtractionKeyword, id=keyword_id, owner=request.user)
    keyword_label = keyword.label
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
    logger.info("extraction_keyword_delete user=%s keyword=%s", request.user.id, keyword_label)
    return redirect("extraction_settings")


@login_required
def process_document_view(request, doc_id):
    if request.method != "POST":
        return HttpResponseForbidden("Método inválido.")

    doc = get_object_or_404(Document, id=doc_id, owner=request.user)
    allow_reprocess = request.POST.get("reprocess") == "1"

    if doc.status == DocumentStatus.PROCESSING:
        return redirect("documents_list")
    if doc.status == DocumentStatus.DONE and not allow_reprocess:
        return redirect("documents_list")

    action = "reprocess" if allow_reprocess else "process"
    force_ocr = _get_force_ocr(request)
    logger.info(
        "process_start doc=%s file=%s action=%s force_ocr=%s",
        doc.id,
        doc.original_filename,
        action,
        force_ocr,
    )
    doc.mark_processing()
    doc.save(
        update_fields=[
            "status",
            "processed_at",
            "error_message",
            "extracted_json",
            "extracted_text",
            "extracted_text_normalized",
            "text_content",
            "text_content_norm",
            "document_type",
            "contact_phone",
            "extracted_age_years",
            "extracted_experience_years",
            "ocr_used",
            "text_quality",
        ]
    )

    try:
        keyword_map = _get_keyword_map(request.user, doc.selected_fields or [])
        data, extracted_text, ocr_used, text_quality = process_document(
            doc.file.path,
            doc.selected_fields or [],
            keyword_map=keyword_map,
            doc_id=str(doc.id),
            filename=doc.original_filename,
            force_ocr=force_ocr,
        )
        _apply_extracted_fields(doc, extracted_text, data)
        doc.mark_done(
            data,
            extracted_text=extracted_text,
            ocr_used=ocr_used,
            text_quality=text_quality,
        )
        doc.save()
        logger.info("process_done doc=%s file=%s action=%s", doc.id, doc.original_filename, action)
    except Exception as exc:
        doc.mark_failed(str(exc))
        doc.save(update_fields=["status", "processed_at", "error_message"])
        logger.exception("process_failed doc=%s file=%s action=%s", doc.id, doc.original_filename, action)

    return redirect("documents_list")


@login_required
def process_documents_bulk(request):
    if request.method != "POST":
        return HttpResponseForbidden("Método inválido.")

    ids = request.POST.getlist("ids")
    if not ids:
        return redirect("documents_list")

    action = request.POST.get("action", "process")
    ids = list(dict.fromkeys(ids))
    ids = ids[:MAX_BULK]

    qs = (
        Document.objects.filter(owner=request.user, id__in=ids)
        .exclude(status=DocumentStatus.PROCESSING)
    )
    if action != "reprocess":
        qs = qs.exclude(status=DocumentStatus.DONE)

    docs = list(qs)
    force_ocr = _get_force_ocr(request)
    logger.info(
        "bulk_process_start user=%s action=%s count=%s force_ocr=%s",
        request.user.id,
        action,
        len(docs),
        force_ocr,
    )
    keyword_fields = []
    for doc in docs:
        keyword_fields.extend(doc.selected_fields or [])
    keyword_map = _get_keyword_map(request.user, keyword_fields)

    for doc in docs:
        doc.mark_processing()
        doc.save(
            update_fields=[
                "status",
                "processed_at",
                "error_message",
                "extracted_json",
                "extracted_text",
                "extracted_text_normalized",
                "text_content",
                "text_content_norm",
                "document_type",
                "contact_phone",
                "extracted_age_years",
                "extracted_experience_years",
                "ocr_used",
                "text_quality",
            ]
        )
        try:
            data, extracted_text, ocr_used, text_quality = process_document(
                doc.file.path,
                doc.selected_fields or [],
                keyword_map=keyword_map,
                doc_id=str(doc.id),
                filename=doc.original_filename,
                force_ocr=force_ocr,
            )
            _apply_extracted_fields(doc, extracted_text, data)
            doc.mark_done(
                data,
                extracted_text=extracted_text,
                ocr_used=ocr_used,
                text_quality=text_quality,
            )
            doc.save()
            logger.info("process_done doc=%s file=%s", doc.id, doc.original_filename)
        except Exception as exc:
            doc.mark_failed(str(exc))
            doc.save(update_fields=["status", "processed_at", "error_message"])
            logger.exception("process_failed doc=%s file=%s", doc.id, doc.original_filename)

    logger.info("bulk_process_end user=%s action=%s count=%s", request.user.id, action, len(docs))
    return redirect("documents_list")


@login_required
def download_document(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, owner=request.user)
    filename = doc.original_filename or os.path.basename(doc.file.name)
    return FileResponse(doc.file.open("rb"), as_attachment=True, filename=filename)


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


def _build_json_filename(doc):
    base_name = doc.original_filename or str(doc.id)
    base_name = os.path.splitext(base_name)[0]
    safe_name = get_valid_filename(base_name) or str(doc.id)
    return f"{safe_name}.json"


@login_required
def download_document_json(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, owner=request.user)
    json_data = sanitize_payload(doc.extracted_json or {})
    payload = json.dumps(json_data, ensure_ascii=False, indent=2)
    filename = _build_json_filename(doc)
    response = HttpResponse(payload, content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def download_documents_json_bulk(request):
    if request.method != "POST":
        return HttpResponseForbidden("Método inválido.")

    ids = request.POST.getlist("ids")
    if not ids:
        return redirect("documents_list")

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
        return redirect("documents_list")

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="documentos-json.zip"'
    logger.info("bulk_json_download user=%s count=%s", request.user.id, added)
    return response


@login_required
def download_documents_files_bulk(request):
    if request.method != "POST":
        return HttpResponseForbidden("Método inválido.")

    ids = request.POST.getlist("ids")
    if not ids:
        return redirect("documents_list")

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
            if not doc.file:
                missing += 1
                continue
            try:
                file_path = doc.file.path
            except Exception:
                missing += 1
                continue
            if not os.path.exists(file_path):
                missing += 1
                continue
            original_name = doc.original_filename or os.path.basename(doc.file.name) or str(doc.id)
            safe_name = _safe_name(original_name, str(doc.id))
            filename = _unique_name(safe_name, used_names, str(doc.id)[:8])
            zip_file.write(file_path, arcname=filename)
            added += 1

    if added == 0:
        return HttpResponse("Nenhum arquivo disponivel para download.", status=400)

    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="documentos-arquivos.zip"'
    logger.info(
        "bulk_files_download user=%s count=%s missing=%s",
        request.user.id,
        added,
        missing,
    )
    return response


@login_required
def document_json_view(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, owner=request.user)
    json_data = sanitize_payload(doc.extracted_json or {})
    return render(request, "documents/json.html", {"doc": doc, "json_data": json_data})
