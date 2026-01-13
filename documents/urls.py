from django.urls import path

from . import views

urlpatterns = [
    path("upload/", views.upload_documents, name="upload_documents"),
    path("", views.documents_list, name="documents_list"),
    path("settings/extraction/", views.extraction_settings, name="extraction_settings"),
    path("settings/extraction/keywords/<int:keyword_id>/delete/", views.delete_keyword, name="delete_keyword"),
    path("process/<uuid:doc_id>/", views.process_document_view, name="process_document"),
    path("process/bulk/", views.process_documents_bulk, name="process_documents_bulk"),
    path("download/<uuid:doc_id>/", views.download_document, name="download_document"),
    path("json/<uuid:doc_id>/", views.document_json_view, name="document_json"),
    path("json/<uuid:doc_id>/download/", views.download_document_json, name="document_json_download"),
    path("json/bulk/", views.download_documents_json_bulk, name="download_documents_json_bulk"),
]
