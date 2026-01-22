import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "automacao_contas.settings")

app = Celery("automacao_contas")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
