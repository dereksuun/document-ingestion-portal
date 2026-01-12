import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

FIELD_CHOICES = [
    ("billing_address", "Endereco de cobranca"),
    ("cnpj", "CNPJ"),
    ("cpf", "CPF"),
    ("payee_name", "Nome do cedente"),
    ("payer_name", "Nome do sacado"),
    ("document_number", "Numero do documento"),
    ("instructions", "Instrucoes"),
]

CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b")
CNPJ_RE = re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{14}\b")
DOC_NUMBER_RE = re.compile(
    r"(?i)(nosso numero|numero do documento|documento)\D{0,10}([0-9A-Z/\.-]{4,})"
)

ADDRESS_LABELS = (
    "endereco",
    "endereco de cobranca",
    "cobranca",
)

ADDRESS_KEYWORDS = (
    "rua",
    "avenida",
    "av ",
    "av.",
    "alameda",
    "travessa",
    "rodovia",
    "estrada",
    "praca",
    "logradouro",
    "lote",
    "quadra",
)

PAYEE_LABELS = (
    "cedente",
    "beneficiario",
    "favorecido",
    "recebedor",
)

PAYEE_BLACKLIST_TERMS = (
    "autenticacao mecanica",
    "beneficiario",
    "data do documento",
    "nosso numero",
    "local de pagamento",
    "numero do documento",
    "recibo do pagador",
    "agencia",
    "banco",
    "carteira",
    "vencimento",
    "valor",
    "pagamento",
    "sacado",
    "pagador",
    "cliente",
    "cpf",
    "cnpj",
    "i.e.",
    "ie:",
    "inscricao estadual",
)

PAYER_LABELS = (
    "sacado",
    "pagador",
    "cliente",
)

INSTRUCTION_KEYWORDS = (
    "instrucao",
    "instrucoes",
    "juros",
    "multa",
    "protesto",
    "apos",
    "nao receber",
    "nao aceitar",
)

COMPANY_SUFFIX_RE = re.compile(
    r"\b([\w][\w\s.\-&]{3,}(?:S\.A\.|S/A|LTDA|Ltda|EIRELI|MEI|ME|EPP))\b",
    re.I,
)

KNOWN_PAYEES_RE = re.compile(
    r"\b(Telefonica Brasil S\.A|Ibagy Imoveis Ltda|Nick Cont Servicos Contabeis Ltda|FJR Telecom|Obvious Fibra)\b",
    re.I,
)

DOC_NUMBER_LABEL_RES = [
    re.compile(r"(?i)nosso numero\D{0,10}([0-9A-Z/\.-]{4,})"),
    re.compile(r"(?i)numero do documento\D{0,10}([0-9A-Z/\.-]{4,})"),
    re.compile(r"(?i)documento\D{0,10}([0-9A-Z/\.-]{4,})"),
    re.compile(r"(?i)numero da conta\D{0,10}(\d{6,})"),
    re.compile(r"(?i)no da conta\D{0,10}(\d{6,})"),
    re.compile(r"(?i)cod(?:igo)?\.?\s*debito\s*automatico\D{0,10}(\d{6,})"),
    re.compile(r"(?i)rps\D{0,10}([0-9A-Z/\.-]{4,})"),
    re.compile(r"(?i)nfs-e\D{0,10}([0-9A-Z/\.-]{4,})"),
    re.compile(r"(?i)fatura\D{0,10}([0-9A-Z/\.-]{4,})"),
]

def _only_digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _normalize_space(value: str) -> str:
    return re.sub(r"\s{2,}", " ", value or "").strip()


def _fold_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    stripped = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return stripped.replace("º", "o").replace("ª", "a").replace("°", "o")


def _format_cpf(cpf: str) -> str:
    return f"{cpf[:3]}.{cpf[3:6]}.{cpf[6:9]}-{cpf[9:]}"


def _format_cnpj(cnpj: str) -> str:
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def _is_valid_cpf(value: str) -> bool:
    digits = _only_digits(value)
    if len(digits) != 11:
        return False
    if digits == digits[0] * 11:
        return False
    for i in range(9, 11):
        total = sum(int(digits[num]) * ((i + 1) - num) for num in range(i))
        check = (total * 10) % 11
        if check == 10:
            check = 0
        if check != int(digits[i]):
            return False
    return True


def _is_valid_cnpj(value: str) -> bool:
    digits = _only_digits(value)
    if len(digits) != 14:
        return False
    if digits == digits[0] * 14:
        return False
    weights_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    weights_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    total_1 = sum(int(d) * w for d, w in zip(digits[:12], weights_1))
    rem_1 = total_1 % 11
    check_1 = 0 if rem_1 < 2 else 11 - rem_1
    total_2 = sum(int(d) * w for d, w in zip(digits[:13], weights_2))
    rem_2 = total_2 % 11
    check_2 = 0 if rem_2 < 2 else 11 - rem_2
    return digits[12] == str(check_1) and digits[13] == str(check_2)


def _find_labeled_value(lines, labels, skip_labels=None):
    skip_labels = skip_labels or []
    folded_skip = [_fold_text(label).lower() for label in skip_labels]
    folded_labels = [_fold_text(label).lower() for label in labels]
    folded_lines = [_fold_text(line).lower() for line in lines]
    for idx, line in enumerate(lines):
        folded_line = folded_lines[idx]
        for label in folded_labels:
            if label in folded_line:
                start = folded_line.index(label) + len(label)
                value = line[start:].strip(" :-\t")
                if value and not _contains_any(_fold_text(value).lower(), folded_skip):
                    return _normalize_space(value)
                if idx + 1 < len(lines):
                    candidate = _normalize_space(lines[idx + 1])
                    if candidate and not _contains_any(_fold_text(candidate).lower(), folded_skip):
                        return candidate
    return None


def _contains_any(value: str, terms) -> bool:
    return any(term in value for term in terms)


def _looks_like_name(value: str) -> bool:
    if not value:
        return False
    if len(value.strip()) < 6:
        return False
    folded = _fold_text(value).lower()
    if _contains_any(folded, PAYEE_BLACKLIST_TERMS):
        return False
    letters = sum(1 for ch in value if ch.isalpha())
    digits = sum(1 for ch in value if ch.isdigit())
    if letters < 3:
        return False
    if digits >= letters and digits > 0:
        return False
    return True


def extract_cpf(text: str) -> dict:
    for match in CPF_RE.findall(text):
        digits = _only_digits(match)
        if _is_valid_cpf(digits):
            return {"cpf": _format_cpf(digits)}
    return {}


def extract_cnpj(text: str) -> dict:
    for match in CNPJ_RE.findall(text):
        digits = _only_digits(match)
        if _is_valid_cnpj(digits):
            return {"cnpj": _format_cnpj(digits)}
    return {}


def extract_billing_address(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    labeled = _find_labeled_value(lines, ADDRESS_LABELS)
    if labeled:
        return {"billing_address": labeled}

    for line in lines:
        lower = line.lower()
        if not any(keyword in lower for keyword in ADDRESS_KEYWORDS):
            continue
        if not any(char.isdigit() for char in line):
            continue
        return {"billing_address": _normalize_space(line)}
    return {}


def extract_payee_name(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    folded_lines = [_fold_text(line).lower() for line in lines]
    value = _find_labeled_value(lines, PAYEE_LABELS, PAYEE_BLACKLIST_TERMS)
    if value and _looks_like_name(value):
        return {"payee_name": value}

    for idx, line in enumerate(folded_lines):
        if any(label in line for label in PAYEE_LABELS):
            for offset in range(1, 4):
                if idx + offset >= len(lines):
                    break
                candidate = _normalize_space(lines[idx + offset])
                if _looks_like_name(candidate):
                    return {"payee_name": candidate}

    for idx, line in enumerate(folded_lines):
        if "cnpj matriz" in line and idx > 0:
            for back in range(1, 4):
                if idx - back < 0:
                    break
                candidate = _normalize_space(lines[idx - back])
                if _looks_like_name(candidate):
                    return {"payee_name": candidate}

    for idx, line in enumerate(folded_lines):
        if "telefonica brasil" in line:
            return {"payee_name": _normalize_space(lines[idx])}

    for line in lines:
        match = COMPANY_SUFFIX_RE.search(line)
        if match:
            candidate = _normalize_space(match.group(1))
            if _looks_like_name(candidate):
                return {"payee_name": candidate}

    known = KNOWN_PAYEES_RE.search(_fold_text(text))
    if known:
        return {"payee_name": _normalize_space(known.group(1))}

    return {}


def extract_payer_name(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    value = _find_labeled_value(lines, PAYER_LABELS)
    if not value:
        return {}
    return {"payer_name": value}


def extract_document_number(text: str) -> dict:
    folded_text = _fold_text(text)
    normalized_text = re.sub(r"\s+", " ", folded_text)
    for regex in DOC_NUMBER_LABEL_RES:
        match = regex.search(folded_text) or regex.search(normalized_text)
        if not match:
            continue
        value = _normalize_space(match.group(1))
        if not value or len(value) < 5:
            continue
        digits = _only_digits(value)
        if len(digits) < 5:
            continue
        if digits and (_is_valid_cnpj(digits) or _is_valid_cpf(digits)):
            continue
        return {"document_number": value}

    match = DOC_NUMBER_RE.search(folded_text) or DOC_NUMBER_RE.search(normalized_text)
    if match:
        value = _normalize_space(match.group(2))
        if not value or len(value) < 5:
            return {}
        digits = _only_digits(value)
        if len(digits) < 5:
            return {}
        if digits and (_is_valid_cnpj(digits) or _is_valid_cpf(digits)):
            return {}
        return {"document_number": value}

    return {}


def extract_instructions(text: str) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected = []
    for line in lines:
        lower = _fold_text(line).lower()
        if any(keyword in lower for keyword in INSTRUCTION_KEYWORDS):
            selected.append(_normalize_space(line))
    if not selected:
        return {}
    deduped = list(dict.fromkeys(selected))
    return {"instructions": " | ".join(deduped)}


FIELD_EXTRACTORS = {
    "billing_address": extract_billing_address,
    "cnpj": extract_cnpj,
    "cpf": extract_cpf,
    "payee_name": extract_payee_name,
    "payer_name": extract_payer_name,
    "document_number": extract_document_number,
    "instructions": extract_instructions,
}
