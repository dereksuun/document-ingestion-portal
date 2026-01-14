from django.db import migrations, models


def seed_fields(apps, schema_editor):
    ExtractionField = apps.get_model("documents", "ExtractionField")
    defaults = [
        ("due_date", "Data de vencimento"),
        ("document_value", "Valor do documento"),
        ("barcode", "Codigo de barras"),
        ("billing_address", "Endereco de cobranca"),
        ("cnpj", "CNPJ"),
        ("cpf", "CPF"),
        ("payee_name", "Nome do cedente"),
        ("payer_name", "Nome do sacado"),
        ("document_number", "Numero do documento"),
        ("instructions", "Instrucoes"),
    ]
    for key, label in defaults:
        ExtractionField.objects.get_or_create(key=key, defaults={"label": label})


def unseed_fields(apps, schema_editor):
    ExtractionField = apps.get_model("documents", "ExtractionField")
    keys = [
        "due_date",
        "document_value",
        "barcode",
        "billing_address",
        "cnpj",
        "cpf",
        "payee_name",
        "payer_name",
        "document_number",
        "instructions",
    ]
    ExtractionField.objects.filter(key__in=keys).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0003_extraction_keyword"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtractionField",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64, unique=True)),
                ("label", models.CharField(max_length=120)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["label"],
            },
        ),
        migrations.RunPython(seed_fields, unseed_fields),
    ]
