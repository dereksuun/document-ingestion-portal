import logging
import os
import re
import shutil
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

from .extractors import FIELD_EXTRACTORS, extract_keyword_value

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
JUROS_LABEL_RE = re.compile(r"(?i)(juros)\D{0,20}([0-9\.]+,[0-9]{2})")
MULTA_LABEL_RE = re.compile(r"(?i)(multa)\D{0,20}([0-9\.]+,[0-9]{2})")

logger = logging.getLogger(__name__)

KEYWORD_PREFIX = "keyword:"
CORE_FIELD_KEYS = {"due_date", "document_value", "barcode", "juros", "multa"}

def _log_field_result(field: str, value):
    if value is None or value == "":
        logger.info("extract_field_missing field=%s", field)
        return
    safe_value = str(value).replace("\n", " ").strip()
    if len(safe_value) > 120:
        safe_value = f"{safe_value[:120]}..."
    logger.info("extract_field_found field=%s value=%s", field, safe_value)


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
            text_parts.append(pytesseract.image_to_string(image, lang=lang) or "")
        else:
            text_parts.append(pytesseract.image_to_string(image) or "")
    text = "\n".join(text_parts).strip()
    if not text:
        raise ValueError("OCR nao conseguiu extrair texto.")
    return text


def extract_text_from_pdf(file_path: str) -> str:
    text = _extract_text_from_pdf(file_path)
    if text:
        return text
    logger.info("ocr_fallback file=%s", os.path.basename(file_path))
    try:
        return _extract_text_with_ocr(file_path)
    except Exception as exc:
        logger.warning("ocr_failed file=%s error=%s", os.path.basename(file_path), exc)
        raise ValueError(f"PDF sem texto selecionavel. OCR falhou: {exc}") from exc


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


def process_document(file_path: str, selected_fields=None, keyword_map=None) -> dict:
    if not file_path.lower().endswith(".pdf"):
        raise ValueError("Suporta apenas PDF.")

    if selected_fields is None:
        selected_fields = []
    selected_fields = list(dict.fromkeys(selected_fields))
    keyword_map = keyword_map or {}

    text = extract_text_from_pdf(file_path)
    result = {
        "document_type": "boleto",
        "raw_text_excerpt": text[:1200],
    }

    missing_fields = []
    resolved_fields = []
    keyword_labels = []
    for field in selected_fields:
        if field.startswith(KEYWORD_PREFIX):
            info = keyword_map.get(field)
            if not info:
                missing_fields.append(field)
                _log_field_result(field, None)
                continue
            field_key = info.get("field_key")
            if field_key:
                resolved_fields.append(field_key)
            else:
                keyword_labels.append(info.get("label", ""))
            continue
        resolved_fields.append(field)
    resolved_fields = list(dict.fromkeys(resolved_fields))
    keyword_labels = [label for label in keyword_labels if label]
    keyword_labels = list(dict.fromkeys(keyword_labels))

    core = None
    if any(field in CORE_FIELD_KEYS for field in resolved_fields):
        core = _extract_core(text)

    if "due_date" in resolved_fields:
        due_date = core["dates"].get("vencimento") if core else None
        if due_date:
            result.setdefault("dates", {})["vencimento"] = due_date
        else:
            missing_fields.append("due_date")
        _log_field_result("due_date", due_date)

    if "document_value" in resolved_fields:
        value = core["amounts"].get("valor_documento") if core else None
        if value:
            result.setdefault("amounts", {})["valor_documento"] = value
        else:
            missing_fields.append("document_value")
        _log_field_result("document_value", value)

    if "barcode" in resolved_fields:
        barcode = core.get("barcode") if core else None
        if barcode and (barcode.get("linha_digitavel") or barcode.get("codigo_barras")):
            result["barcode"] = barcode
        else:
            missing_fields.append("barcode")
        _log_field_result(
            "barcode",
            (barcode or {}).get("codigo_barras") or (barcode or {}).get("linha_digitavel"),
        )

    if "juros" in resolved_fields:
        juros = core["amounts"].get("juros") if core else None
        if juros:
            result.setdefault("amounts", {})["juros"] = juros
        else:
            missing_fields.append("juros")
        _log_field_result("juros", juros)

    if "multa" in resolved_fields:
        multa = core["amounts"].get("multa") if core else None
        if multa:
            result.setdefault("amounts", {})["multa"] = multa
        else:
            missing_fields.append("multa")
        _log_field_result("multa", multa)

    for field in resolved_fields:
        if field in CORE_FIELD_KEYS:
            continue
        extractor = FIELD_EXTRACTORS.get(field)
        if not extractor:
            missing_fields.append(field)
            _log_field_result(field, None)
            continue
        piece = extractor(text)
        if not piece:
            missing_fields.append(field)
            _log_field_result(field, None)
            continue
        result.update(piece)
        _log_field_result(field, piece.get(field))

    custom_fields = {}
    for label in keyword_labels:
        value = extract_keyword_value(text, label)
        if value:
            custom_fields[label] = value
            _log_field_result(label, value)
        else:
            missing_fields.append(label)
            _log_field_result(label, None)

    if custom_fields:
        result["custom_fields"] = custom_fields

    result["extraction"] = {
        "selected_fields": list(dict.fromkeys(resolved_fields + keyword_labels)),
        "missing_fields": missing_fields,
    }
    return result
