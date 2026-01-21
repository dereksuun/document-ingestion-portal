from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0012_document_fields"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="filterpreset",
            name="document_type",
        ),
    ]
