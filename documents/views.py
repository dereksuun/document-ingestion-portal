import io
import json
import logging
import os
import zipfile

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import FileResponse, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import get_valid_filename

from .forms import ExtractionSettingsForm, KeywordForm, MultiUploadForm
from .intent import resolve_intent
from .models import (
    Document,
    DocumentStatus,
    ExtractionField,
    ExtractionKeyword,
    ExtractionProfile,
    STRATEGY_CHOICES,
    VALUE_TYPE_CHOICES,
    _normalize_keyword,
)
from .services import KEYWORD_PREFIX, process_document, sanitize_payload

PAGE_SIZE = 10
MAX_BULK = 25

logger = logging.getLogger(__name__)


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
                doc.save(update_fields=["status", "processed_at", "error_message", "extracted_json"])
                try:
                    data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map, doc_id=str(doc.id), filename=doc.original_filename)
                    doc.mark_done(data)
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
    docs = (
        Document.objects.filter(owner=request.user)
        .order_by("-uploaded_at")
    )
    paginator = Paginator(docs, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(request, "documents/list.html", {"page_obj": page_obj})


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
    logger.info("process_start doc=%s file=%s action=%s", doc.id, doc.original_filename, action)
    doc.mark_processing()
    doc.save(update_fields=["status", "processed_at", "error_message", "extracted_json"])

    try:
        keyword_map = _get_keyword_map(request.user, doc.selected_fields or [])
        data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map, doc_id=str(doc.id), filename=doc.original_filename)
        doc.mark_done(data)
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
    logger.info("bulk_process_start user=%s action=%s count=%s", request.user.id, action, len(docs))
    keyword_fields = []
    for doc in docs:
        keyword_fields.extend(doc.selected_fields or [])
    keyword_map = _get_keyword_map(request.user, keyword_fields)

    for doc in docs:
        doc.mark_processing()
        doc.save(update_fields=["status", "processed_at", "error_message", "extracted_json"])
        try:
            data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map, doc_id=str(doc.id), filename=doc.original_filename)
            doc.mark_done(data)
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
def document_json_view(request, doc_id):
    doc = get_object_or_404(Document, id=doc_id, owner=request.user)
    json_data = sanitize_payload(doc.extracted_json or {})
    return render(request, "documents/json.html", {"doc": doc, "json_data": json_data})
