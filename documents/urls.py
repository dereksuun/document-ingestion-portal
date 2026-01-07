from django.urls import path

from . import views

urlpatterns = [
    path("upload/", views.upload_documents, name="upload_documents"),
    path("", views.documents_list, name="documents_list"),
    path("process/<uuid:doc_id>/", views.process_document_view, name="process_document"),
    path("download/<uuid:doc_id>/", views.download_document, name="download_document"),
    path("json/<uuid:doc_id>/", views.document_json_view, name="document_json"),
]
