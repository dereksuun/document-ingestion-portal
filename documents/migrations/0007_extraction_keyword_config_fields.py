from django.db import migrations, models


VALUE_TYPES = {
    "text",
    "block",
    "money",
    "date",
    "cpf",
    "cnpj",
    "id",
    "barcode",
    "address",
}


def backfill_keyword_config(apps, schema_editor):
    ExtractionKeyword = apps.get_model("documents", "ExtractionKeyword")
    for keyword in ExtractionKeyword.objects.all().iterator():
        updates = {}
        inferred_type = (keyword.inferred_type or "text").lower()
        value_type = inferred_type if inferred_type in VALUE_TYPES else "text"
        if not keyword.value_type:
            updates["value_type"] = value_type
        if not keyword.strategy:
            updates["strategy"] = "after_label"
        if keyword.strategy_params is None:
            updates["strategy_params"] = {}
        if updates:
            ExtractionKeyword.objects.filter(pk=keyword.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0006_extraction_keyword_intent_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionkeyword",
            name="value_type",
            field=models.CharField(
                choices=[
                    ("text", "Texto"),
                    ("block", "Bloco"),
                    ("money", "Valor"),
                    ("date", "Data"),
                    ("cpf", "CPF"),
                    ("cnpj", "CNPJ"),
                    ("id", "Identificador"),
                    ("barcode", "Codigo de barras"),
                    ("address", "Endereco"),
                ],
                default="text",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="extractionkeyword",
            name="strategy",
            field=models.CharField(
                choices=[
                    ("after_label", "Depois do rotulo"),
                    ("next_line", "Proxima linha"),
                    ("below_n_lines", "Abaixo de N linhas"),
                    ("regex", "Regex"),
                    ("nearest_match", "Mais proximo"),
                ],
                default="after_label",
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="extractionkeyword",
            name="strategy_params",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.RunPython(backfill_keyword_config, migrations.RunPython.noop),
    ]
