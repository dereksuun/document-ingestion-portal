import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

LINE_CANDIDATE_RE = re.compile(r"(?:\d[\s\.\-]?){44,48}")
DATE_LABEL_RE = re.compile(
    r"(?i)(vencimento|vcto|vencto|data de vencimento)\D{0,20}([0-3]?\d[\./-][01]?\d[\./-](?:\d{2}|\d{4}))"
)
EMISSAO_LABEL_RE = re.compile(
    r"(?i)(emiss[aã]o|data de emiss[aã]o)\D{0,20}([0-3]?\d[\./-][01]?\d[\./-](?:\d{2}|\d{4}))"
)
AMOUNT_LABEL_RE = re.compile(
    r"(?i)(valor(?: do documento)?|valor cobrado|valor a pagar|total)\D{0,20}([0-9\.]+,[0-9]{2})"
)
GENERIC_AMOUNT_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]{3})*,[0-9]{2})")
JUROS_LABEL_RE = re.compile(r"(?i)(juros)\D{0,20}([0-9\.]+,[0-9]{2})")
MULTA_LABEL_RE = re.compile(r"(?i)(multa)\D{0,20}([0-9\.]+,[0-9]{2})")


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


def _extract_line_candidates(text):
    candidates = []
    for match in LINE_CANDIDATE_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) in {44, 47, 48}:
            candidates.append(digits)
    return candidates


def _select_barcode_and_line(candidates):
    line_digitavel = None
    barcode = None
    for cand in candidates:
        if len(cand) in {47, 48} and not line_digitavel:
            line_digitavel = cand
        if len(cand) == 44 and not barcode:
            barcode = cand
    return line_digitavel, barcode


def extract_text_from_pdf(file_path: str) -> str:
    reader = PdfReader(file_path)
    text_parts = []
    for page in reader.pages:
        text_parts.append(page.extract_text() or "")
    text = "\n".join(text_parts).strip()
    if not text:
        raise ValueError("PDF sem texto selecionável (provavelmente escaneado). OCR entra na V2.")
    return text


def process_document(file_path: str) -> dict:
    if not file_path.lower().endswith(".pdf"):
        raise ValueError("V1 suporta apenas PDF (texto). OCR entra na V2.")

    text = extract_text_from_pdf(file_path)

    candidates = _extract_line_candidates(text)
    line_digitavel, barcode = _select_barcode_and_line(candidates)

    vencimento = _parse_date(_extract_first(DATE_LABEL_RE, text) or "")
    emissao = _parse_date(_extract_first(EMISSAO_LABEL_RE, text) or "")

    valor = _extract_amount(AMOUNT_LABEL_RE, text)
    if not valor:
        generic_amount = GENERIC_AMOUNT_RE.search(text)
        if generic_amount:
            valor = _parse_amount(generic_amount.group(1))

    juros = _extract_amount(JUROS_LABEL_RE, text)
    multa = _extract_amount(MULTA_LABEL_RE, text)

    return {
        "document_type": "boleto",
        "raw_text_excerpt": text[:1200],
        "dates": {"vencimento": vencimento, "emissao": emissao},
        "amounts": {"valor_documento": valor, "juros": juros, "multa": multa},
        "barcode": {"linha_digitavel": line_digitavel, "codigo_barras": barcode},
        "billing_address": None,
    }
