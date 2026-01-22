from .models import Document, ExtractionKeyword
from .services import (
    KEYWORD_PREFIX,
    _normalize_for_match,
    extract_age_years,
    extract_contact_phone,
    extract_experience_years,
)


def get_keyword_map(owner_id, selected_fields):
    keyword_ids = []
    for field in selected_fields or []:
        if not field.startswith(KEYWORD_PREFIX):
            continue
        raw_id = field.split(":", 1)[1]
        if not raw_id.isdigit():
            continue
        keyword_ids.append(int(raw_id))
    if not keyword_ids:
        return {}
    keywords = ExtractionKeyword.objects.filter(owner_id=owner_id, id__in=keyword_ids)
    mapping = {}
    for keyword in keywords:
        mapping[f"{KEYWORD_PREFIX}{keyword.id}"] = {
            "label": keyword.label,
            "resolved_kind": keyword.resolved_kind,
            "field_key": keyword.field_key,
            "inferred_type": keyword.inferred_type,
            "value_type": keyword.value_type,
            "strategy": keyword.strategy,
            "strategy_params": keyword.strategy_params or {},
            "anchors": keyword.anchors or [],
            "match_strategy": keyword.match_strategy,
            "confidence": keyword.confidence,
        }
    return mapping


def apply_extracted_fields(doc: Document, extracted_text: str, payload: dict):
    text_value = extracted_text or ""
    normalized = _normalize_for_match(text_value)
    doc.extracted_text_normalized = normalized
    doc.text_content = text_value
    doc.text_content_norm = normalized
    doc.document_type = (payload or {}).get("document_type") or ""
    doc.contact_phone = extract_contact_phone(text_value)
    doc.extracted_age_years = extract_age_years(text_value)
    doc.extracted_experience_years = extract_experience_years(text_value)
