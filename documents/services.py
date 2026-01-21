import logging
import os
import re
import shutil
import time
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

from .extractors import FIELD_EXTRACTORS, PAYER_SCOPE_ANCHORS, extract_cnpj, extract_cpf
from .intent_catalog import TYPE_BY_BUILTIN

try:
    from pdf2image import convert_from_path
except ImportError:
    convert_from_path = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

LINE_47_GROUP_RE = re.compile(
    r"\b(\d{5})\.(\d{5})\s+(\d{5})\.(\d{6})\s+(\d{5})\.(\d{6})\s+(\d)\s+(\d{14})\b"
)
LINE_48_GROUP_RE = re.compile(r"\b(\d{12})[\s\.]+(\d{12})[\s\.]+(\d{12})[\s\.]+(\d{12})\b")
LINE_CANDIDATE_RE = re.compile(r"(?:\d[\s\.\-]?){44,48}")
DATE_LABEL_RE = re.compile(
    r"(?i)(vencimento|vcto|vencto|data de vencimento)\D{0,20}([0-3]?\d[\./-][01]?\d[\./-](?:\d{4}|\d{2}))"
)
EMISSAO_LABEL_RE = re.compile(
    r"(?i)(emiss[aã]o|data de emiss[aã]o)\D{0,20}([0-3]?\d[\./-][01]?\d[\./-](?:\d{4}|\d{2}))"
)
AMOUNT_LABEL_RE = re.compile(
    r"(?i)(valor(?: do documento)?|valor cobrado|valor a pagar|total)\D{0,20}([0-9\.]+,[0-9]{2})"
)
GENERIC_AMOUNT_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})")
GENERIC_DATE_RE = re.compile(r"\b([0-3]?\d[\./-][01]?\d[\./-](?:\d{4}|\d{2}))\b")
GENERIC_ID_RE = re.compile(r"\b[0-9A-Z]{5,}\b")
CEP_RE = re.compile(r"\b\d{5}-?\d{3}\b")
JUROS_LABEL_RE = re.compile(r"(?i)(juros)\D{0,20}([0-9\.]+,[0-9]{2})")
MULTA_LABEL_RE = re.compile(r"(?i)(multa)\D{0,20}([0-9\.]+,[0-9]{2})")
PHONE_CANDIDATE_RE = re.compile(r"(?:\+?\d[\d\-\.\(\)\s]{8,}\d)")
DOB_RE = re.compile(
    r"(?i)(?:data\s+de\s+nascimento|nascimento|nasc\.?)\D{0,10}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})"
)
AGE_INLINE_RE = re.compile(r"(?i)\b(\d{1,2})\s+anos?\s+de\s+idade\b")
AGE_LABEL_RE = re.compile(r"(?i)\bidade\D{0,6}(\d{1,2})\b")
EXPERIENCE_RE = re.compile(r"(?i)\b(\d{1,2})\s+anos?\s+(?:de\s+)?experiencia\b")
EXPERIENCE_LABEL_RE = re.compile(r"(?i)\bexperiencia\D{0,10}(\d{1,2})\s*anos?\b")

logger = logging.getLogger(__name__)

KEYWORD_PREFIX = "keyword:"
CORE_FIELD_KEYS = {"due_date", "document_value", "barcode", "juros", "multa"}
BUILTIN_FIELD_KEYS = set(TYPE_BY_BUILTIN.keys())

ALIAS_FIELD_KEYS = {
    "cnpj": "payee_cnpj",
    "billing_address": "payer_address",
}

CUSTOM_CONTEXT_LINES = 3

CONTEXT_LINES_BY_TYPE = {
    "money": 3,
    "date": 3,
    "id": 3,
    "text": 4,
    "address": 5,
    "block": 6,
    "barcode": 2,
    "cpf": 2,
    "cnpj": 2,
    "postal": 2,
}

CUSTOM_STOP_PHRASES = (
    "local de pagamento",
    "nosso numero",
    "numero do documento",
    "numero da conta",
    "linha digitavel",
    "codigo de barras",
    "data de vencimento",
    "data de emissao",
    "data do documento",
    "vencimento",
    "valor do documento",
    "valor total",
    "valor a pagar",
    "juros",
    "multa",
    "autenticacao mecanica",
    "recibo do pagador",
    "cedente",
    "sacado",
    "pagador",
    "beneficiario",
    "instrucoes",
    "cpf",
    "cnpj",
)

OCR_TESSERACT_CONFIG = "--psm 6"


def _normalize_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().lower()


_CUSTOM_STOP_NORMS = {_normalize_for_match(value) for value in CUSTOM_STOP_PHRASES}


def _coerce_year(value: int) -> int:
    if value >= 100:
        return value
    current_year = date.today().year
    pivot = current_year % 100
    return (2000 + value) if value <= pivot else (1900 + value)


def _valid_years(value: int, *, min_value: int = 0, max_value: int = 120) -> int | None:
    if value < min_value or value > max_value:
        return None
    return value


def extract_contact_phone(text: str) -> str | None:
    if not text:
        return None
    seen = set()
    best = None
    best_score = -1
    for match in PHONE_CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        if digits in seen:
            continue
        seen.add(digits)
        if len(digits) in {12, 13} and not digits.startswith("55"):
            continue
        if len(digits) not in {10, 11, 12, 13}:
            continue
        has_country = digits.startswith("55")
        local = digits[2:] if has_country else digits
        if len(local) not in {10, 11}:
            continue
        is_mobile = len(local) == 11
        score = 0
        if has_country:
            score += 3
        if raw.strip().startswith("+"):
            score += 1
        if is_mobile:
            score += 2
        if score > best_score:
            best_score = score
            best = digits
    if not best:
        return None
    if best.startswith("55"):
        return best
    return f"55{best}"


def extract_age_years(text: str) -> int | None:
    if not text:
        return None
    normalized = _normalize_for_match(text)
    match = DOB_RE.search(normalized)
    if match:
        raw = match.group(1)
        parts = re.split(r"[/-]", raw)
        if len(parts) == 3:
            try:
                day, month, year = (int(item) for item in parts)
            except ValueError:
                day = month = year = None
            if day and month and year:
                year = _coerce_year(year)
                try:
                    birth_date = date(year, month, day)
                except ValueError:
                    birth_date = None
                if birth_date:
                    today = date.today()
                    age = today.year - birth_date.year
                    if (today.month, today.day) < (birth_date.month, birth_date.day):
                        age -= 1
                    return _valid_years(age)

    match = AGE_INLINE_RE.search(normalized) or AGE_LABEL_RE.search(normalized)
    if match:
        try:
            age_value = int(match.group(1))
        except ValueError:
            age_value = None
        if age_value is not None:
            return _valid_years(age_value)

    for match in re.finditer(r"\b(\d{1,2})\s+anos?\b", normalized):
        window = normalized[max(0, match.start() - 20) : match.end() + 20]
        if "idade" not in window:
            continue
        try:
            age_value = int(match.group(1))
        except ValueError:
            continue
        age_value = _valid_years(age_value)
        if age_value is not None:
            return age_value
    return None


def extract_experience_years(text: str) -> int | None:
    if not text:
        return None
    normalized = _normalize_for_match(text)
    match = EXPERIENCE_RE.search(normalized) or EXPERIENCE_LABEL_RE.search(normalized)
    if not match:
        return None
    try:
        years_value = int(match.group(1))
    except ValueError:
        return None
    return _valid_years(years_value, min_value=0, max_value=60)


def _context_lines_for_type(inferred_type: str) -> int:
    inferred_type = (inferred_type or "").lower()
    return CONTEXT_LINES_BY_TYPE.get(inferred_type, CUSTOM_CONTEXT_LINES)


def _collect_anchor_lines(text: str, anchors, context_lines: int):
    anchors = [anchor for anchor in (anchors or []) if anchor]
    if not anchors:
        return []
    norm_anchors = [_normalize_for_match(anchor) for anchor in anchors]
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    norm_lines = [_normalize_for_match(line) for line in lines]
    selected = []
    for idx, norm_line in enumerate(norm_lines):
        if any(anchor in norm_line for anchor in norm_anchors):
            selected.append(lines[idx])
            for offset in range(1, context_lines + 1):
                if idx + offset < len(lines):
                    selected.append(lines[idx + offset])
    return list(dict.fromkeys(selected))


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _find_anchor_indexes(lines, anchors) -> list[int]:
    anchors = [anchor for anchor in (anchors or []) if anchor]
    if not anchors:
        return []
    norm_anchors = [_normalize_for_match(anchor) for anchor in anchors]
    indexes = []
    for idx, line in enumerate(lines):
        norm_line = _normalize_for_match(line)
        if any(anchor in norm_line for anchor in norm_anchors):
            indexes.append(idx)
    return indexes


def _extract_after_label(line: str, anchors):
    anchors = [anchor for anchor in (anchors or []) if anchor]
    for anchor in anchors:
        match = re.search(re.escape(anchor), line, re.IGNORECASE)
        if not match:
            continue
        value = line[match.end():].strip(" :-\t")
        if value:
            return value
    return None


def _next_non_empty_line(lines, start_idx: int):
    for idx in range(start_idx, len(lines)):
        value = lines[idx].strip()
        if value:
            return value
    return None


def _looks_like_section_title(value: str) -> bool:
    cleaned = (value or "").strip()
    if not cleaned:
        return False
    if cleaned.endswith(":"):
        return True
    if cleaned.isupper() and len(cleaned) <= 40:
        return True
    return False


def _extract_block(lines, max_lines: int):
    if not lines:
        return None
    collected = []
    for line in lines[: max(1, max_lines)]:
        value = line.strip()
        if not value:
            continue
        if collected and _looks_like_section_title(value):
            break
        collected.append(value)
    if not collected:
        return None
    return "\n".join(collected)


def _extract_value_from_lines(lines, anchors, value_type: str, max_lines: int | None = None):
    value_type = (value_type or "text").lower()
    if value_type in {"money", "amount"}:
        return _extract_money_from_lines(lines)
    if value_type == "date":
        return _extract_date_from_lines(lines)
    if value_type == "cpf":
        return extract_cpf("\n".join(lines)).get("cpf")
    if value_type == "cnpj":
        return extract_cnpj("\n".join(lines)).get("cnpj")
    if value_type == "barcode":
        return _extract_barcode_from_text("\n".join(lines))
    if value_type == "postal":
        return _extract_postal_from_lines(lines)
    if value_type == "id":
        value = _extract_id_from_lines(lines)
        if value and _is_noise_value(value, anchors, value_type):
            return None
        return value
    if value_type == "block":
        return _extract_block(lines, max_lines or _context_lines_for_type(value_type))

    value = _extract_text_from_lines(lines, anchors)
    if not value:
        value = _next_non_empty_line(lines, 0)
    if value and _is_noise_value(value, anchors, value_type):
        return None
    return value


def _log_custom_attempt(field_key, value_type, strategy, anchors, window_size, found):
    logger.info(
        "custom_extract field=%s type=%s strategy=%s anchors=%s window=%s found=%s",
        field_key,
        value_type,
        strategy,
        anchors,
        window_size,
        found,
    )

def _extract_text_from_lines(lines, anchors):
    anchors = [anchor for anchor in (anchors or []) if anchor]
    for line in lines:
        for anchor in anchors:
            match = re.search(re.escape(anchor), line, re.IGNORECASE)
            if not match:
                continue
            value = line[match.end():].strip(" :-\t")
            if value:
                return value
    return lines[0] if lines else None


def _looks_like_anchor(value_norm: str, anchors) -> bool:
    anchor_norms = [_normalize_for_match(anchor) for anchor in (anchors or []) if anchor]
    for anchor in anchor_norms:
        if not anchor:
            continue
        if value_norm == anchor:
            return True
        if value_norm.startswith(anchor) and len(value_norm) <= len(anchor) + 2:
            return True
    return False


def _looks_like_label(value_norm: str) -> bool:
    if not value_norm:
        return True
    for phrase in _CUSTOM_STOP_NORMS:
        if not phrase:
            continue
        if value_norm == phrase:
            return True
        if value_norm.startswith(phrase) and len(value_norm) <= len(phrase) + 2:
            return True
    return False


def _count_letters_digits(value: str) -> tuple[int, int]:
    letters = sum(1 for ch in value if ch.isalpha())
    digits = sum(1 for ch in value if ch.isdigit())
    return letters, digits


def _looks_like_amount_or_date(value: str) -> bool:
    if GENERIC_AMOUNT_RE.search(value):
        return True
    if GENERIC_DATE_RE.search(value):
        return True
    return False


def _is_noise_value(value: str, anchors, inferred_type: str) -> bool:
    value_norm = _normalize_for_match(value or "")
    if not value_norm or len(value_norm) < 3:
        return True
    if _looks_like_anchor(value_norm, anchors):
        return True
    if _looks_like_label(value_norm):
        return True

    inferred_type = (inferred_type or "text").lower()
    if inferred_type in {"text", "address"}:
        if _looks_like_amount_or_date(value):
            return True
        letters, digits = _count_letters_digits(value)
        if letters < 3:
            return True
        if digits >= letters and digits > 0:
            return True
    if inferred_type == "id":
        compact = re.sub(r"[^0-9A-Za-z]", "", value)
        if len(compact) < 4:
            return True
    return False


def _extract_money_from_lines(lines):
    candidates = []
    for line in lines:
        for match in GENERIC_AMOUNT_RE.findall(line):
            amount = _parse_amount_decimal(match)
            if amount is not None:
                candidates.append(amount)
    if not candidates:
        return None
    best = max(candidates)
    return str(best.quantize(Decimal("0.01")))


def _extract_date_from_lines(lines):
    for line in lines:
        match = GENERIC_DATE_RE.search(line)
        if not match:
            continue
        value = _parse_date(match.group(1))
        if value:
            return value
    return None


def _extract_id_from_lines(lines):
    best = ""
    for line in lines:
        for match in GENERIC_ID_RE.findall(line):
            if len(match) > len(best):
                best = match
    return best or None


def _extract_postal_from_lines(lines):
    for line in lines:
        match = CEP_RE.search(line)
        if match:
            return match.group(0)
    return None


def _extract_barcode_from_text(text):
    candidates = _extract_line_candidates(text)
    line_digitavel, barcode = _select_barcode_and_line(candidates)
    return line_digitavel or barcode


def extract_custom(keyword_def: dict, text: str):
    anchors = keyword_def.get("anchors") or []
    label = (keyword_def.get("label") or "").strip()
    if not anchors and label:
        anchors = [label]

    value_type = (keyword_def.get("value_type") or keyword_def.get("inferred_type") or "text").lower()
    if value_type not in {"text", "block", "money", "date", "cpf", "cnpj", "id", "barcode", "address", "postal"}:
        value_type = "text"
    strategy = (keyword_def.get("strategy") or "after_label").lower()
    if strategy not in {"after_label", "next_line", "below_n_lines", "regex", "nearest_match"}:
        strategy = "after_label"
    params = keyword_def.get("strategy_params") or {}
    if not isinstance(params, dict):
        params = {}

    lines = _split_lines(text)
    anchor_indexes = _find_anchor_indexes(lines, anchors)
    window_size = None

    if strategy == "regex":
        pattern = params.get("pattern")
        if not pattern:
            _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
            return None
        try:
            match = re.search(pattern, text, flags=re.IGNORECASE)
        except re.error:
            _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
            return None
        if not match:
            _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
            return None
        value = match.group(1) if match.groups() else match.group(0)
        if value and _is_noise_value(value, anchors, value_type):
            value = None
        _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, bool(value))
        return value

    if not anchor_indexes:
        _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
        return None

    if strategy == "after_label":
        for idx in anchor_indexes:
            value = _extract_after_label(lines[idx], anchors)
            if value and _is_noise_value(value, anchors, value_type):
                value = None
            if value:
                _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, 0, True)
                return value
        _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, 0, False)
        return None

    if strategy == "next_line":
        for idx in anchor_indexes:
            value = _next_non_empty_line(lines, idx + 1)
            if value and _is_noise_value(value, anchors, value_type):
                value = None
            if value:
                _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, 1, True)
                return value
        _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, 1, False)
        return None

    if strategy in {"below_n_lines", "nearest_match"}:
        max_lines = params.get("max_lines")
        try:
            max_lines = int(max_lines)
        except (TypeError, ValueError):
            max_lines = _context_lines_for_type(value_type)
        max_lines = max(1, max_lines)
        window_size = max_lines
        for idx in anchor_indexes:
            if strategy == "nearest_match":
                start = max(0, idx - max_lines)
                end = min(len(lines), idx + max_lines + 1)
                context_lines = lines[start:end]
            else:
                start = min(idx + 1, len(lines))
                end = min(len(lines), start + max_lines)
                context_lines = lines[start:end]
            value = _extract_value_from_lines(context_lines, anchors, value_type, max_lines)
            if value:
                _log_custom_attempt(
                    keyword_def.get("keyword_key") or label,
                    value_type,
                    strategy,
                    anchors,
                    window_size,
                    True,
                )
                return value
        _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
        return None

    _log_custom_attempt(keyword_def.get("keyword_key") or label, value_type, strategy, anchors, window_size, False)
    return None


def extract_missing_with_llm(text: str, fields):
    return {}


def classify_document_type(text: str):
    if not text:
        return None

    candidates = _extract_line_candidates(text)
    line_digitavel, barcode = _select_barcode_and_line(candidates)
    if line_digitavel or barcode:
        return "boleto"

    normalized = _normalize_for_match(text)
    if not normalized:
        return None

    doc_signals = [
        ("boleto", ["boleto", "linha digitavel", "codigo de barras"]),
        ("nota_fiscal", ["nota fiscal", "nf-e", "nfe", "danfe"]),
        ("fatura", ["fatura", "invoice"]),
        ("recibo", ["recibo"]),
        (
            "comprovante",
            ["comprovante", "comprovacao", "comprovante de pagamento", "pix", "transferencia", "transacao"],
        ),
    ]

    for doc_type, keywords in doc_signals:
        if any(keyword in normalized for keyword in keywords):
            return doc_type

    return None


FORBIDDEN_PAYLOAD_KEYS = {
    "extraction",
    "raw_text_excerpt",
    "selected_fields",
    "selected_fields_raw",
    "resolved_fields",
    "missing_fields",
    "ocr_used",
    "duration_ms",
    "anchors",
    "match_strategy",
    "confidence",
}


def sanitize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"document_type": None, "fields": {}, "custom_fields": {}}

    removed = [key for key in FORBIDDEN_PAYLOAD_KEYS if key in payload]
    if removed:
        logger.error("payload_forbidden_keys keys=%s", removed)

    document_type = payload.get("document_type")

    fields: dict[str, object] = {}
    raw_fields = payload.get("fields") or {}
    if isinstance(raw_fields, dict):
        for key in BUILTIN_FIELD_KEYS:
            if key in raw_fields:
                fields[key] = raw_fields.get(key)

    dates = payload.get("dates") or {}
    if isinstance(dates, dict):
        if "vencimento" in dates and "due_date" not in fields:
            fields["due_date"] = dates.get("vencimento")

    amounts = payload.get("amounts") or {}
    if isinstance(amounts, dict):
        if "valor_documento" in amounts and "document_value" not in fields:
            fields["document_value"] = amounts.get("valor_documento")
        if "juros" in amounts and "juros" not in fields:
            fields["juros"] = amounts.get("juros")
        if "multa" in amounts and "multa" not in fields:
            fields["multa"] = amounts.get("multa")

    barcode = payload.get("barcode") or {}
    if isinstance(barcode, dict) and "barcode" not in fields:
        barcode_value = barcode.get("linha_digitavel") or barcode.get("codigo_barras")
        if barcode_value:
            fields["barcode"] = barcode_value

    for key in BUILTIN_FIELD_KEYS:
        if key in payload and key not in fields:
            fields[key] = payload.get(key)

    legacy_map = {
        "cnpj": "payee_cnpj",
        "billing_address": "payer_address",
    }
    for legacy_key, new_key in legacy_map.items():
        if legacy_key in fields and new_key not in fields:
            fields[new_key] = fields.get(legacy_key)
        if legacy_key in fields:
            fields.pop(legacy_key, None)

    custom_fields: dict[str, dict] = {}
    raw_custom = payload.get("custom_fields") or {}
    if isinstance(raw_custom, dict):
        for key, value in raw_custom.items():
            custom_key = str(key)
            if isinstance(value, dict):
                label = value.get("label") or custom_key
                custom_fields[custom_key] = {"label": label, "value": value.get("value")}
            else:
                custom_fields[custom_key] = {"label": custom_key, "value": value}

    return {
        "document_type": document_type,
        "fields": fields,
        "custom_fields": custom_fields,
    }

def _mask_log_value(value, inferred_type: str):
    safe_value = str(value).replace("\n", " ").strip()
    inferred_type = (inferred_type or "").lower()
    if inferred_type in {"cpf", "cnpj"}:
        digits = re.sub(r"\D", "", safe_value)
        if len(digits) >= 4:
            return f"***{digits[-4:]}"
        return "***"
    if inferred_type == "barcode":
        digits = re.sub(r"\D", "", safe_value)
        if len(digits) >= 6:
            return f"len={len(digits)} tail={digits[-6:]}"
        return f"len={len(digits)}"
    if inferred_type in {"text", "address", "block"}:
        return f"len={len(safe_value)}"
    if inferred_type == "id":
        compact = re.sub(r"[^0-9A-Za-z]", "", safe_value)
        if len(compact) >= 4:
            return f"len={len(compact)} tail={compact[-4:]}"
        return f"len={len(compact)}"
    if len(safe_value) > 120:
        return f"{safe_value[:120]}..."
    return safe_value


def _log_field_result(field: str, value, *, strategy: str, inferred_type: str = "", match_strategy: str = "", label: str = ""):
    status = "extract_ok" if value not in (None, "") else "extract_missing"
    safe_value = _mask_log_value(value, inferred_type) if status == "extract_ok" else "-"
    logger.info(
        "%s field=%s strategy=%s type=%s match=%s label=%s value=%s",
        status,
        field,
        strategy,
        inferred_type or "-",
        match_strategy or "-",
        label or "-",
        safe_value,
    )


def _parse_date(value: str):
    cleaned = value.strip().replace("-", "/").replace(".", "/")
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_amount(value: str):
    cleaned = value.strip().replace(".", "").replace(",", ".")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return str(amount.quantize(Decimal("0.01")))


def _parse_amount_decimal(value: str):
    cleaned = value.strip().replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _extract_first(regex, text):
    match = regex.search(text)
    if not match:
        return None
    return match.group(2)


def _extract_amount(regex, text):
    match = regex.search(text)
    if not match:
        return None
    return _parse_amount(match.group(2))


def _extract_amount_by_context(text):
    contextual = []
    for line in text.splitlines():
        lower = line.lower()
        if any(term in lower for term in ("valor", "total", "a pagar", "pagar", "documento")):
            for match in GENERIC_AMOUNT_RE.findall(line):
                amount = _parse_amount_decimal(match)
                if amount is not None:
                    contextual.append(amount)
    if contextual:
        best = max(contextual)
        return str(best.quantize(Decimal("0.01")))

    candidates = []
    for match in GENERIC_AMOUNT_RE.findall(text):
        amount = _parse_amount_decimal(match)
        if amount is not None:
            candidates.append(amount)
    if not candidates:
        return None
    best = max(candidates)
    return str(best.quantize(Decimal("0.01")))


def _extract_line_candidates(text):
    candidates = []
    for match in LINE_47_GROUP_RE.findall(text):
        digits = "".join(match)
        if len(digits) == 47:
            candidates.append(digits)
    for match in LINE_48_GROUP_RE.findall(text):
        digits = "".join(match)
        if len(digits) == 48:
            candidates.append(digits)
    for line in text.splitlines():
        for match in LINE_CANDIDATE_RE.findall(line):
            digits = re.sub(r"\D", "", match)
            if len(digits) in {44, 47, 48}:
                candidates.append(digits)
    return list(dict.fromkeys(candidates))


def _select_barcode_and_line(candidates):
    line_digitavel = None
    barcode = None
    for cand in candidates:
        if len(cand) in {47, 48} and not line_digitavel:
            line_digitavel = cand
        if len(cand) == 44 and not barcode:
            barcode = cand
    return line_digitavel, barcode


def _extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    return "\n".join(text_parts).strip()

MIN_TEXT_CHARS = 200
MIN_TEXT_WORDS = 30


def _text_quality_stats(text: str) -> tuple[int, int]:
    stripped = (text or "").strip()
    if not stripped:
        return 0, 0
    word_count = len(re.findall(r"\w+", stripped))
    char_count = len(re.sub(r"\s+", "", stripped))
    return word_count, char_count


def _text_is_weak(word_count: int, char_count: int) -> bool:
    if word_count == 0 and char_count == 0:
        return True
    return char_count < MIN_TEXT_CHARS or word_count < MIN_TEXT_WORDS


def _missing_ocr_deps():
    missing = []
    if convert_from_path is None:
        missing.append("pdf2image")
    if pytesseract is None:
        missing.append("pytesseract")
    if shutil.which("tesseract") is None:
        missing.append("tesseract-ocr")
    if shutil.which("pdftoppm") is None:
        missing.append("poppler-utils (pdftoppm)")
    return missing


def _extract_text_with_ocr(file_path: str) -> str:
    missing = _missing_ocr_deps()
    if missing:
        raise RuntimeError("OCR nao disponivel. Instale: " + ", ".join(missing))

    try:
        images = convert_from_path(file_path, dpi=300)
    except Exception as exc:
        raise RuntimeError(f"OCR falhou ao converter PDF em imagens: {exc}") from exc

    if not images:
        raise ValueError("OCR falhou: PDF sem paginas.")

    lang = os.getenv("OCR_LANG")
    text_parts = []
    for image in images:
        if lang:
            text_parts.append(
                pytesseract.image_to_string(image, lang=lang, config=OCR_TESSERACT_CONFIG) or ""
            )
        else:
            text_parts.append(pytesseract.image_to_string(image, config=OCR_TESSERACT_CONFIG) or "")
    text = "\n".join(text_parts).strip()
    if not text:
        raise ValueError("OCR nao conseguiu extrair texto.")
    return text


def extract_text_with_ocr_flag(file_path: str, *, force_ocr: bool = False) -> tuple[str, bool, int]:
    if force_ocr:
        logger.info("ocr_forced file=%s", os.path.basename(file_path))
        ocr_text = _extract_text_with_ocr(file_path)
        ocr_word_count, _ = _text_quality_stats(ocr_text)
        return ocr_text, True, ocr_word_count

    extraction_error = None
    try:
        text = _extract_text_from_pdf(file_path)
    except Exception as exc:
        extraction_error = exc
        text = ""
    word_count, char_count = _text_quality_stats(text)
    if extraction_error is None and not _text_is_weak(word_count, char_count):
        return text, False, word_count
    if extraction_error is not None:
        logger.warning(
            "pdf_text_extract_failed file=%s error=%s",
            os.path.basename(file_path),
            extraction_error,
        )
        reason = "extract_error"
    else:
        reason = "weak_text"
    logger.info(
        "ocr_fallback file=%s reason=%s chars=%s words=%s",
        os.path.basename(file_path),
        reason,
        char_count,
        word_count,
    )
    try:
        ocr_text = _extract_text_with_ocr(file_path)
    except Exception as exc:
        if text.strip():
            logger.warning(
                "ocr_failed file=%s error=%s fallback=keep_pdf_text",
                os.path.basename(file_path),
                exc,
            )
            return text, False, word_count
        logger.warning("ocr_failed file=%s error=%s", os.path.basename(file_path), exc)
        raise ValueError(f"PDF sem texto selecionavel. OCR falhou: {exc}") from exc
    ocr_word_count, _ = _text_quality_stats(ocr_text)
    return ocr_text, True, ocr_word_count


def extract_text_from_pdf(file_path: str) -> str:
    text, _, _ = extract_text_with_ocr_flag(file_path)
    return text


def _extract_core(text: str) -> dict:
    candidates = _extract_line_candidates(text)
    line_digitavel, barcode = _select_barcode_and_line(candidates)

    vencimento = _parse_date(_extract_first(DATE_LABEL_RE, text) or "")
    emissao = _parse_date(_extract_first(EMISSAO_LABEL_RE, text) or "")

    valor = _extract_amount(AMOUNT_LABEL_RE, text)
    if not valor:
        valor = _extract_amount_by_context(text)

    juros = _extract_amount(JUROS_LABEL_RE, text)
    multa = _extract_amount(MULTA_LABEL_RE, text)

    return {
        "document_type": "boleto",
        "raw_text_excerpt": text[:1200],
        "dates": {"vencimento": vencimento, "emissao": emissao},
        "amounts": {"valor_documento": valor, "juros": juros, "multa": multa},
        "barcode": {"linha_digitavel": line_digitavel, "codigo_barras": barcode},
    }


def process_document(
    file_path: str,
    selected_fields=None,
    keyword_map=None,
    *,
    doc_id: str | None = None,
    filename: str | None = None,
    force_ocr: bool = False,
) -> tuple[dict, str, bool, int]:
    if not file_path.lower().endswith(".pdf"):
        raise ValueError("Suporta apenas PDF.")

    started_at = time.monotonic()
    if selected_fields is None:
        selected_fields = []
    selected_fields = list(dict.fromkeys(selected_fields))
    keyword_map = keyword_map or {}

    text, ocr_used, text_quality = extract_text_with_ocr_flag(file_path, force_ocr=force_ocr)
    storage_text = text
    storage_quality = text_quality
    file_label = filename or os.path.basename(file_path)
    logger.info(
        "process_document_start doc=%s file=%s selected=%s ocr=%s force_ocr=%s",
        doc_id or "-",
        file_label,
        selected_fields,
        ocr_used,
        force_ocr,
    )

    payload = {
        "document_type": classify_document_type(text) or None,
        "fields": {},
        "custom_fields": {},
    }

    missing_fields = []
    missing_resolved_fields = []
    resolved_fields = []
    raw_fields_by_builtin = {}
    custom_definitions = []
    for field in selected_fields:
        if field.startswith(KEYWORD_PREFIX):
            info = keyword_map.get(field) or {}
            if not info:
                missing_fields.append(field)
                _log_field_result(field, None, strategy="keyword")
                continue
            resolved_kind = (info.get("resolved_kind") or "custom").lower()
            field_key = info.get("field_key") or ""
            field_key = ALIAS_FIELD_KEYS.get(field_key, field_key)
            if resolved_kind == "builtin" and field_key:
                resolved_fields.append(field_key)
                raw_fields_by_builtin.setdefault(field_key, []).append(field)
            else:
                custom_info = dict(info)
                custom_info["keyword_key"] = field
                custom_definitions.append(custom_info)
            continue
        canonical_field = ALIAS_FIELD_KEYS.get(field, field)
        resolved_fields.append(canonical_field)
        raw_fields_by_builtin.setdefault(canonical_field, []).append(field)
    resolved_fields = list(dict.fromkeys(resolved_fields))

    core = None
    if any(field in CORE_FIELD_KEYS for field in resolved_fields):
        core = _extract_core(text)

    def _mark_missing_builtin(field_key: str):
        missing_resolved_fields.append(field_key)
        raw_fields = raw_fields_by_builtin.get(field_key) or [field_key]
        missing_fields.extend(raw_fields)
        if field_key.startswith("payer_"):
            logger.info(
                "extract_missing_reason field=%s reason=not_found_in_payer_block anchors=%s ocr=%s",
                field_key,
                list(PAYER_SCOPE_ANCHORS),
                ocr_used,
            )

    def _log_builtin_field(field_key: str, value):
        inferred_type = TYPE_BY_BUILTIN.get(field_key, "")
        raw_fields = raw_fields_by_builtin.get(field_key) or [field_key]
        for raw_field in raw_fields:
            if raw_field.startswith(KEYWORD_PREFIX):
                info = keyword_map.get(raw_field) or {}
                label = info.get("label") or ""
                match_strategy = info.get("match_strategy") or ""
                _log_field_result(
                    raw_field,
                    value,
                    strategy="builtin",
                    inferred_type=inferred_type,
                    match_strategy=match_strategy,
                    label=label,
                )
            else:
                _log_field_result(
                    field_key,
                    value,
                    strategy="builtin",
                    inferred_type=inferred_type,
                )

    if "due_date" in resolved_fields:
        due_date = core["dates"].get("vencimento") if core else None
        payload["fields"]["due_date"] = due_date if due_date else None
        if not due_date:
            _mark_missing_builtin("due_date")
        _log_builtin_field("due_date", due_date)

    if "document_value" in resolved_fields:
        value = core["amounts"].get("valor_documento") if core else None
        payload["fields"]["document_value"] = value if value else None
        if not value:
            _mark_missing_builtin("document_value")
        _log_builtin_field("document_value", value)

    if "barcode" in resolved_fields:
        barcode = core.get("barcode") if core else None
        barcode_value = (barcode or {}).get("codigo_barras") or (barcode or {}).get("linha_digitavel")
        payload["fields"]["barcode"] = barcode_value if barcode_value else None
        if not (barcode and (barcode.get("linha_digitavel") or barcode.get("codigo_barras"))):
            _mark_missing_builtin("barcode")
        _log_builtin_field("barcode", barcode_value)

    if "juros" in resolved_fields:
        juros = core["amounts"].get("juros") if core else None
        payload["fields"]["juros"] = juros if juros else None
        if not juros:
            _mark_missing_builtin("juros")
        _log_builtin_field("juros", juros)

    if "multa" in resolved_fields:
        multa = core["amounts"].get("multa") if core else None
        payload["fields"]["multa"] = multa if multa else None
        if not multa:
            _mark_missing_builtin("multa")
        _log_builtin_field("multa", multa)

    for field in resolved_fields:
        if field in CORE_FIELD_KEYS:
            continue
        extractor = FIELD_EXTRACTORS.get(field)
        if not extractor:
            _mark_missing_builtin(field)
            payload["fields"][field] = None
            _log_builtin_field(field, None)
            continue
        piece = extractor(text)
        if not piece:
            _mark_missing_builtin(field)
            payload["fields"][field] = None
            _log_builtin_field(field, None)
            continue
        value = piece.get(field)
        payload["fields"][field] = value if value else None
        _log_builtin_field(field, value)

    payer_missing = [field for field in missing_resolved_fields if field.startswith("payer_")]
    if payer_missing and not ocr_used:
        try:
            ocr_text = _extract_text_with_ocr(file_path)
        except Exception as exc:
            logger.warning(
                "ocr_on_demand_failed doc=%s file=%s fields=%s error=%s",
                doc_id or "-",
                file_label,
                payer_missing,
                exc,
            )
        else:
            ocr_used = True
            storage_text = ocr_text
            storage_quality = _text_quality_stats(ocr_text)[0]
            logger.info(
                "ocr_on_demand doc=%s file=%s fields=%s",
                doc_id or "-",
                file_label,
                payer_missing,
            )

            def _clear_missing(field_key: str):
                raw_fields = raw_fields_by_builtin.get(field_key) or [field_key]
                missing_resolved_fields[:] = [item for item in missing_resolved_fields if item != field_key]
                missing_fields[:] = [item for item in missing_fields if item not in raw_fields]

            for field in payer_missing:
                extractor = FIELD_EXTRACTORS.get(field)
                if not extractor:
                    continue
                piece = extractor(ocr_text)
                value = piece.get(field) if piece else None
                if value:
                    payload["fields"][field] = value
                    _clear_missing(field)
                    _log_builtin_field(field, value)
                else:
                    logger.info(
                        "extract_missing_reason field=%s reason=not_found_in_payer_block anchors=%s ocr=%s",
                        field,
                        list(PAYER_SCOPE_ANCHORS),
                        True,
                    )
                    _log_builtin_field(field, None)

    for info in custom_definitions:
        keyword_key = info.get("keyword_key") or ""
        label = (info.get("label") or "").strip()
        inferred_type = info.get("inferred_type") or "text"
        value_type = info.get("value_type") or inferred_type
        match_strategy = info.get("match_strategy") or ""
        value = extract_custom(info, text)
        field_key = keyword_key or label or inferred_type
        payload["custom_fields"][field_key] = {
            "label": label or field_key,
            "value": value if value else None,
        }
        if value:
            _log_field_result(
                field_key,
                value,
                strategy="custom",
                inferred_type=value_type,
                match_strategy=match_strategy,
                label=label,
            )
        else:
            missing_fields.append(field_key)
            _log_field_result(
                field_key,
                None,
                strategy="custom",
                inferred_type=value_type,
                match_strategy=match_strategy,
                label=label,
            )

    missing_fields = list(dict.fromkeys(missing_fields))
    missing_resolved = list(dict.fromkeys(missing_resolved_fields))
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    logger.info(
        "process_document_done doc=%s file=%s duration_ms=%s missing=%s missing_resolved=%s resolved=%s ocr=%s",
        doc_id or "-",
        file_label,
        elapsed_ms,
        missing_fields,
        missing_resolved,
        resolved_fields,
        ocr_used,
    )
    return sanitize_payload(payload), storage_text, ocr_used, storage_quality
