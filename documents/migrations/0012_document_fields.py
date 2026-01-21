import re
import unicodedata

from django.db import migrations, models


def _normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def backfill_document_text(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    for doc in Document.objects.all().iterator():
        updated = []
        extracted_text = doc.extracted_text or ""
        if not doc.text_content and extracted_text:
            doc.text_content = extracted_text
            updated.append("text_content")

        if not doc.text_content_norm:
            if doc.extracted_text_normalized:
                doc.text_content_norm = doc.extracted_text_normalized
            elif extracted_text:
                doc.text_content_norm = _normalize_for_match(extracted_text)
            if doc.text_content_norm:
                updated.append("text_content_norm")

        if not doc.document_type and doc.extracted_json and isinstance(doc.extracted_json, dict):
            doc_type = (doc.extracted_json or {}).get("document_type") or ""
            if doc_type:
                doc.document_type = doc_type
                updated.append("document_type")

        if updated:
            doc.save(update_fields=updated)


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0011_filter_preset"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="contact_phone",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="document_type",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="document",
            name="extracted_age_years",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="extracted_experience_years",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="document",
            name="text_content",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="document",
            name="text_content_norm",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill_document_text, migrations.RunPython.noop),
    ]
