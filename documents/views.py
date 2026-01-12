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

from .forms import ExtractionSettingsForm, MultiUploadForm
from .models import Document, DocumentStatus, ExtractionProfile
from .services import process_document

PAGE_SIZE = 10
MAX_BULK = 25

logger = logging.getLogger(__name__)


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
            selected_fields = list(profile.enabled_fields or [])
            logger.info(
                "upload_documents user=%s count=%s files=%s",
                request.user.id,
                len(files),
                filenames,
            )
            with transaction.atomic():
                for file_obj in files:
                    Document.objects.create(
                        owner=request.user,
                        file=file_obj,
                        original_filename=file_obj.name,
                        selected_fields=selected_fields,
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
def extraction_settings(request):
    profile = _get_profile(request.user)
    if request.method == "POST":
        form = ExtractionSettingsForm(request.POST)
        if form.is_valid():
            enabled_fields = form.cleaned_data["enabled_fields"]
            profile.enabled_fields = enabled_fields
            profile.save(update_fields=["enabled_fields", "updated_at"])
            logger.info("extraction_profile_update user=%s fields=%s", request.user.id, enabled_fields)
            return redirect("extraction_settings")
    else:
        form = ExtractionSettingsForm(initial={"enabled_fields": profile.enabled_fields})

    return render(request, "documents/settings.html", {"form": form})


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
        data = process_document(doc.file.path, doc.selected_fields or [])
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

    for doc in docs:
        doc.mark_processing()
        doc.save(update_fields=["status", "processed_at", "error_message", "extracted_json"])
        try:
            data = process_document(doc.file.path, doc.selected_fields or [])
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
def process_documents_pending(request):
    if request.method != "POST":
        return HttpResponseForbidden("Método inválido.")

    qs = (
        Document.objects.filter(
            owner=request.user,
            status__in=[DocumentStatus.PENDING, DocumentStatus.FAILED],
        )
        .exclude(status=DocumentStatus.PROCESSING)
        .order_by("-uploaded_at")[:MAX_BULK]
    )
    docs = list(qs)
    if not docs:
        return redirect("documents_list")

    logger.info("pending_process_start user=%s count=%s", request.user.id, len(docs))

    for doc in docs:
        doc.mark_processing()
        doc.save(update_fields=["status", "processed_at", "error_message", "extracted_json"])
        try:
            data = process_document(doc.file.path, doc.selected_fields or [])
            doc.mark_done(data)
            doc.save()
            logger.info("process_done doc=%s file=%s action=pending", doc.id, doc.original_filename)
        except Exception as exc:
            doc.mark_failed(str(exc))
            doc.save(update_fields=["status", "processed_at", "error_message"])
            logger.exception("process_failed doc=%s file=%s action=pending", doc.id, doc.original_filename)

    logger.info("pending_process_end user=%s count=%s", request.user.id, len(docs))
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
