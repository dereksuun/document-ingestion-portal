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
from .models import (
    Document,
    DocumentStatus,
    ExtractionField,
    ExtractionKeyword,
    ExtractionProfile,
    _normalize_keyword,
)
from .services import KEYWORD_PREFIX, process_document

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
    return [value for value in (enabled_fields or []) if value in allowed]


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
            "field_key": keyword.field_key,
        }
    return mapping


FIELD_ALIASES = {
    "vencimento": "due_date",
    "data de vencimento": "due_date",
    "valor": "document_value",
    "valor do documento": "document_value",
    "codigo de barras": "barcode",
    "linha digitavel": "barcode",
    "local de cobranca": "billing_address",
    "endereco de cobranca": "billing_address",
    "juros": "juros",
    "multa": "multa",
}


def _resolve_field_key(label):
    normalized = _normalize_keyword(label)
    if not normalized:
        return "", ""
    fields = ExtractionField.objects.all()
    for field in fields:
        if normalized == _normalize_keyword(field.key) or normalized == _normalize_keyword(field.label):
            return field.key, field.label
    return FIELD_ALIASES.get(normalized, ""), ""


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
                    data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map)
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
    if request.method == "POST":
        form = ExtractionSettingsForm(request.POST, choices=choices)
        keyword_form = KeywordForm(request.POST)
        action = request.POST.get("action", "save")
        if action == "add_keyword" and keyword_form.is_valid():
            keyword_value = keyword_form.cleaned_data.get("new_keyword") or ""
            normalized = _normalize_keyword(keyword_value)
            field_key, matched_label = _resolve_field_key(keyword_value)
            matches_field = bool(matched_label) and normalized == _normalize_keyword(matched_label)
            if not keyword_value:
                keyword_form.add_error("new_keyword", "Informe uma palavra-chave.")
            elif normalized in {"", None}:
                keyword_form.add_error("new_keyword", "Informe uma palavra-chave valida.")
            elif matches_field:
                enabled_fields = (
                    form.cleaned_data["enabled_fields"] if form.is_valid() else current_fields
                )
                enabled_fields = list(enabled_fields)
                if field_key not in enabled_fields:
                    enabled_fields.append(field_key)
                    profile.enabled_fields = enabled_fields
                    profile.save(update_fields=["enabled_fields", "updated_at"])
                return redirect("extraction_settings")
            elif ExtractionKeyword.objects.filter(
                owner=request.user, normalized_label=normalized
            ).exists():
                keyword_form.add_error("new_keyword", "Essa palavra-chave ja existe.")
            else:
                keyword = ExtractionKeyword.objects.create(
                    owner=request.user,
                    label=keyword_value,
                    field_key=field_key,
                )
                enabled_fields = (
                    form.cleaned_data["enabled_fields"] if form.is_valid() else current_fields
                )
                enabled_fields = list(enabled_fields)
                enabled_fields.append(f"{KEYWORD_PREFIX}{keyword.id}")
                profile.enabled_fields = enabled_fields
                profile.save(update_fields=["enabled_fields", "updated_at"])
                logger.info(
                    "extraction_keyword_add user=%s keyword=%s",
                    request.user.id,
                    keyword.label,
                )
                return redirect("extraction_settings")

        if action != "add_keyword" and form.is_valid():
            enabled_fields = form.cleaned_data["enabled_fields"]
            profile.enabled_fields = enabled_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])
            logger.info("extraction_profile_update user=%s fields=%s", request.user.id, enabled_fields)
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
        data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map)
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
            data = process_document(doc.file.path, doc.selected_fields or [], keyword_map=keyword_map)
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
    json_data = doc.extracted_json or {}
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
            payload = json.dumps(doc.extracted_json, ensure_ascii=False, indent=2)
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
    json_data = doc.extracted_json or {}
    return render(request, "documents/json.html", {"doc": doc, "json_data": json_data})
