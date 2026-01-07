import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


class DocumentStatus(models.TextChoices):
    PENDING = "PENDING", "Pendente"
    PROCESSING = "PROCESSING", "Processando"
    DONE = "DONE", "Processado"
    FAILED = "FAILED", "Falhou"


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
