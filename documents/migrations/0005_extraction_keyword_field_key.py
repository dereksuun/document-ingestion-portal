import re
import unicodedata

from django.db import migrations, models


def _normalize(value: str) -> str:
    raw = (value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", raw)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped)


def add_extra_fields(apps, schema_editor):
    ExtractionField = apps.get_model("documents", "ExtractionField")
    defaults = [
        ("juros", "Juros"),
        ("multa", "Multa"),
    ]
    for key, label in defaults:
        ExtractionField.objects.get_or_create(key=key, defaults={"label": label})


def remove_extra_fields(apps, schema_editor):
    ExtractionField = apps.get_model("documents", "ExtractionField")
    ExtractionField.objects.filter(key__in=["juros", "multa"]).delete()


def backfill_keyword_field_keys(apps, schema_editor):
    ExtractionKeyword = apps.get_model("documents", "ExtractionKeyword")
    ExtractionField = apps.get_model("documents", "ExtractionField")

    field_map = {}
    for field in ExtractionField.objects.all():
        field_map[_normalize(field.key)] = field.key
        field_map[_normalize(field.label)] = field.key

    aliases = {
        "vencimento": "due_date",
        "data de vencimento": "due_date",
        "valor": "document_value",
        "valor do documento": "document_value",
        "codigo de barras": "barcode",
        "linha digitavel": "barcode",
        "local de cobranca": "billing_address",
        "endereco de cobranca": "billing_address",
    }

    for keyword in ExtractionKeyword.objects.all():
        normalized = _normalize(keyword.label)
        field_key = field_map.get(normalized) or aliases.get(normalized, "")
        if field_key and keyword.field_key != field_key:
            keyword.field_key = field_key
            keyword.save(update_fields=["field_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0004_extraction_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionkeyword",
            name="field_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(add_extra_fields, remove_extra_fields),
        migrations.RunPython(backfill_keyword_field_keys, reverse_code=migrations.RunPython.noop),
    ]
