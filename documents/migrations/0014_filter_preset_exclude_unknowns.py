from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0013_filter_preset_remove_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="filterpreset",
            name="exclude_unknowns",
            field=models.BooleanField(default=False),
        ),
    ]
