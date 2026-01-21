import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0010_document_extracted_text_normalized"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FilterPreset",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("scope", models.CharField(choices=[("private", "Privado"), ("team", "Equipe"), ("global", "Global")], default="private", max_length=16)),
                ("document_type", models.CharField(blank=True, default="", max_length=40)),
                ("keywords", models.JSONField(default=list)),
                ("keywords_mode", models.CharField(choices=[("all", "Todos"), ("any", "Qualquer")], default="all", max_length=8)),
                ("experience_min_years", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("experience_max_years", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("age_min_years", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("age_max_years", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="filter_presets", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["name"],
            },
        ),
    ]
