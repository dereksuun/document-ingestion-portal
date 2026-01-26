import logging
import os
import tempfile

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Document, DocumentStatus
from .processing import apply_extracted_fields, get_keyword_map
from .services import process_document

logger = logging.getLogger(__name__)

PROCESSING_UPDATE_FIELDS = [
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


def _prepare_document_file(doc):
    try:
        return doc.file.path, None
    except (AttributeError, NotImplementedError, ValueError):
        pass

    if not doc.file or not doc.file.name:
        raise FileNotFoundError("document file not available")

    file_name = doc.file.name
    suffix = os.path.splitext(file_name)[1]
    tmp_path = None
    file_obj = doc.file.storage.open(file_name, "rb")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp_file:
            tmp_path = tmp_file.name
            for chunk in _iter_file_chunks(file_obj):
                tmp_file.write(chunk)
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
        raise
    finally:
        file_obj.close()

    def _cleanup():
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass

    return tmp_path, _cleanup


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def process_document_task(self, doc_id, *, force=False, force_ocr=False):
    try:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(id=doc_id)

            if doc.status == DocumentStatus.PROCESSING:
                logger.info("task_skip doc=%s reason=already_processing", doc_id)
                return {"skipped": True, "reason": "already_processing"}

            if doc.status == DocumentStatus.DONE and not force:
                logger.info("task_skip doc=%s reason=already_done", doc_id)
                return {"skipped": True, "reason": "already_done"}

            doc.mark_processing()
            doc.save(update_fields=PROCESSING_UPDATE_FIELDS)

            selected_fields = doc.selected_fields or []
            owner_id = doc.owner_id
            filename = doc.original_filename
    except Document.DoesNotExist:
        logger.warning("task_skip doc=%s reason=missing", doc_id)
        return {"skipped": True, "reason": "missing"}

    cleanup = None
    try:
        file_path, cleanup = _prepare_document_file(doc)
        keyword_map = get_keyword_map(owner_id, selected_fields)
        data, extracted_text, ocr_used, text_quality = process_document(
            file_path,
            selected_fields,
            keyword_map=keyword_map,
            doc_id=str(doc_id),
            filename=filename,
            force_ocr=force_ocr,
        )
    except Exception as exc:
        with transaction.atomic():
            updated = Document.objects.filter(id=doc_id).update(
                status=DocumentStatus.FAILED,
                processed_at=timezone.now(),
                error_message=(str(exc) or "")[:5000],
            )
        if not updated:
            logger.warning("process_failed doc=%s reason=missing", doc_id)
            return {"skipped": True, "reason": "missing"}
        logger.exception("process_failed doc=%s task=%s", doc_id, getattr(self.request, "id", "-"))
        raise
    finally:
        if cleanup:
            cleanup()

    with transaction.atomic():
        try:
            doc = Document.objects.select_for_update().get(id=doc_id)
        except Document.DoesNotExist:
            logger.warning("process_done doc=%s reason=missing", doc_id)
            return {"skipped": True, "reason": "missing"}
        apply_extracted_fields(doc, extracted_text, data)
        doc.mark_done(
            data,
            extracted_text=extracted_text,
            ocr_used=ocr_used,
            text_quality=text_quality,
        )
        doc.save()
        logger.info("process_done doc=%s task=%s", doc_id, getattr(self.request, "id", "-"))

    return {"ok": True}
