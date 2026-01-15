from django.db import migrations


def update_payer_payee_fields(apps, schema_editor):
    ExtractionField = apps.get_model("documents", "ExtractionField")
    ExtractionKeyword = apps.get_model("documents", "ExtractionKeyword")
    ExtractionProfile = apps.get_model("documents", "ExtractionProfile")
    Document = apps.get_model("documents", "Document")

    def _rename_field(old_key, new_key, new_label):
        field = ExtractionField.objects.filter(key=old_key).first()
        existing = ExtractionField.objects.filter(key=new_key).first()
        if field:
            if existing and existing.pk != field.pk:
                field.delete()
            else:
                field.key = new_key
                field.label = new_label
                field.save(update_fields=["key", "label"])
        else:
            ExtractionField.objects.get_or_create(key=new_key, defaults={"label": new_label})
        if existing and existing.label != new_label:
            existing.label = new_label
            existing.save(update_fields=["label"])

    _rename_field("cnpj", "payee_cnpj", "CNPJ do cedente")
    _rename_field("billing_address", "payer_address", "Endereco do pagador")

    for key, label in (
        ("payer_cnpj", "CNPJ do pagador"),
        ("payee_address", "Endereco do cedente"),
        ("payee_cnpj", "CNPJ do cedente"),
        ("payer_address", "Endereco do pagador"),
    ):
        ExtractionField.objects.get_or_create(key=key, defaults={"label": label})
        ExtractionField.objects.filter(key=key).update(label=label)

    field_map = {
        "cnpj": "payee_cnpj",
        "billing_address": "payer_address",
    }

    for keyword in ExtractionKeyword.objects.filter(field_key__in=field_map.keys()).iterator():
        keyword.field_key = field_map.get(keyword.field_key, keyword.field_key)
        keyword.save(update_fields=["field_key"])

    for profile in ExtractionProfile.objects.all().iterator():
        current = profile.enabled_fields or []
        mapped = [field_map.get(value, value) for value in current]
        deduped = list(dict.fromkeys(mapped))
        if deduped != current:
            profile.enabled_fields = deduped
            profile.save(update_fields=["enabled_fields"])

    for doc in Document.objects.all().iterator():
        current = doc.selected_fields or []
        mapped = [field_map.get(value, value) for value in current]
        deduped = list(dict.fromkeys(mapped))
        if deduped != current:
            doc.selected_fields = deduped
            doc.save(update_fields=["selected_fields"])


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0007_extraction_keyword_config_fields"),
    ]

    operations = [
        migrations.RunPython(update_payer_payee_fields, migrations.RunPython.noop),
    ]
