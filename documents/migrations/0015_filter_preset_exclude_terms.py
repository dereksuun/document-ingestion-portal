from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0014_filter_preset_exclude_unknowns"),
    ]

    operations = [
        migrations.AddField(
            model_name="filterpreset",
            name="exclude_terms_text",
            field=models.TextField(blank=True, default=""),
        ),
    ]
