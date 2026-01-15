import re
import unicodedata
import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()

VALUE_TYPE_CHOICES = [
    ("text", "Texto"),
    ("block", "Bloco"),
    ("money", "Valor"),
    ("date", "Data"),
    ("cpf", "CPF"),
    ("cnpj", "CNPJ"),
    ("id", "Identificador"),
    ("barcode", "Codigo de barras"),
    ("address", "Endereco"),
]

STRATEGY_CHOICES = [
    ("after_label", "Depois do rotulo"),
    ("next_line", "Proxima linha"),
    ("below_n_lines", "Abaixo de N linhas"),
    ("regex", "Regex"),
    ("nearest_match", "Mais proximo"),
]


class DocumentStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    PROCESSING = "PROCESSING", "Processando"
    DONE = "DONE", "Processado"
    FAILED = "FAILED", "Falhou"


class ExtractionProfile(models.Model):
    owner = models.OneToOneField(User, on_delete=models.CASCADE, related_name="extraction_profile")
    enabled_fields = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"ExtractionProfile({self.owner_id})"


class ExtractionField(models.Model):
    key = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return f"ExtractionField({self.key})"


def _normalize_keyword(value: str) -> str:
    raw = (value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", raw)
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    cleaned = re.sub(r"\s+", " ", stripped)
    return cleaned


class ExtractionKeyword(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="extraction_keywords")
    label = models.CharField(max_length=120)
    field_key = models.CharField(max_length=64, blank=True, default="")
    resolved_kind = models.CharField(max_length=16, default="custom")
    inferred_type = models.CharField(max_length=32, blank=True, default="")
    value_type = models.CharField(
        max_length=24,
        choices=VALUE_TYPE_CHOICES,
        default="text",
    )
    strategy = models.CharField(
        max_length=24,
        choices=STRATEGY_CHOICES,
        default="after_label",
    )
    strategy_params = models.JSONField(default=dict, blank=True)
    anchors = models.JSONField(default=list)
    match_strategy = models.CharField(max_length=24, blank=True, default="")
    confidence = models.FloatField(default=0.0)
    normalized_label = models.CharField(max_length=160, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("owner", "normalized_label")
        ordering = ["label"]

    def save(self, *args, **kwargs):
        self.normalized_label = _normalize_keyword(self.label)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"ExtractionKeyword({self.owner_id}, {self.label})"


class Document(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="documents")
    uploaded_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=DocumentStatus.choices,
        default=DocumentStatus.PENDING,
    )

    file = models.FileField(upload_to="documents/%Y/%m/%d/")
    original_filename = models.CharField(max_length=255)
    stored_path = models.CharField(max_length=500, blank=True)

    selected_fields = models.JSONField(default=list)
    extracted_json = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    def save(self, *args, **kwargs):
        if self.file and not self.stored_path:
            self.stored_path = self.file.name
        super().save(*args, **kwargs)

    def mark_processing(self):
        self.status = DocumentStatus.PROCESSING
        self.processed_at = None
        self.error_message = ""
        self.extracted_json = None

    def mark_done(self, data: dict):
        self.extracted_json = data
        self.status = DocumentStatus.DONE
        self.processed_at = timezone.now()
        self.error_message = ""

    def mark_failed(self, msg: str):
        self.status = DocumentStatus.FAILED
        self.processed_at = timezone.now()
        self.error_message = (msg or "")[:5000]

    def __str__(self):
        return f"{self.original_filename} ({self.id})"
