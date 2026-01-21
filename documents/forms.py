import re

from django import forms

from .models import FilterPreset
from .services import _normalize_for_match


VALUE_TYPE_CHOICES = [
    ("text", "text"),
    ("money", "money"),
    ("date", "date"),
    ("cpf", "cpf"),
    ("cnpj", "cnpj"),
    ("id", "id"),
    ("barcode", "barcode"),
    ("address", "address"),
    ("block", "block"),
]


MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    def clean(self, data, initial=None):
        if not data and initial:
            return initial
        if data and isinstance(data, (list, tuple)):
            return [super().clean(item, initial) for item in data]
        return super().clean(data, initial)


class MultiUploadForm(forms.Form):
    files = MultiFileField(
        widget=MultiFileInput(attrs={"multiple": True, "class": "input-file"}),
        required=True,
        label="Documentos",
    )

    def clean_files(self):
        files = self.cleaned_data["files"]
        if not isinstance(files, (list, tuple)):
            files = [files]
        for file_obj in files:
            name = (getattr(file_obj, "name", "") or "").lower()
            content_type = (getattr(file_obj, "content_type", "") or "").lower()
            is_pdf = name.endswith(".pdf") or content_type == "application/pdf"
            if not is_pdf:
                raise forms.ValidationError("Apenas arquivos PDF sao permitidos.")
            if getattr(file_obj, "size", 0) > MAX_FILE_SIZE_BYTES:
                raise forms.ValidationError(
                    f"O arquivo '{file_obj.name}' excede {MAX_FILE_SIZE_MB} MB."
                )
        return files


class ExtractionSettingsForm(forms.Form):
    enabled_fields = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Campos opcionais",
    )

    def __init__(self, *args, choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        if choices is not None:
            self.fields["enabled_fields"].choices = choices


class KeywordForm(forms.Form):
    new_keyword = forms.CharField(
        required=False,
        max_length=120,
        label="Adicionar palavra-chave",
        widget=forms.TextInput(
            attrs={
                "class": "input-text",
                "list": "field-suggestions",
                "placeholder": "Adicionar palavra (ex: Sao Paulo, CNPJ)",
                "autocomplete": "off",
            }
        ),
    )
    value_type = forms.ChoiceField(
        required=False,
        choices=VALUE_TYPE_CHOICES,
        initial="text",
        label="Tipo do valor",
        widget=forms.Select(
            attrs={
                "class": "input-select",
                "aria-label": "Tipo do valor",
            }
        ),
    )
    strategy = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )
    strategy_params = forms.CharField(
        required=False,
        widget=forms.HiddenInput,
    )

    def clean_new_keyword(self):
        value = (self.cleaned_data.get("new_keyword") or "").strip()
        if not value:
            return ""
        return " ".join(value.split())


TERM_SPLIT_RE = re.compile(r"[,\s]+")


def _split_keywords(raw: str) -> list[str]:
    if not raw:
        return []
    if ";" in raw:
        parts = [term.strip() for term in raw.split(";") if term.strip()]
    else:
        parts = [term.strip() for term in TERM_SPLIT_RE.split(raw) if term.strip()]
    normalized_terms = []
    seen = set()
    for term in parts:
        normalized = _normalize_for_match(term)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        normalized_terms.append(normalized)
    return normalized_terms


class FilterPresetForm(forms.ModelForm):
    keywords_text = forms.CharField(
        required=False,
        label="Palavras-chave",
        widget=forms.Textarea(
            attrs={
                "class": "input-text",
                "rows": 3,
                "placeholder": "Separe termos com ;",
            }
        ),
    )

    class Meta:
        model = FilterPreset
        fields = [
            "name",
            "scope",
            "document_type",
            "keywords_mode",
            "experience_min_years",
            "experience_max_years",
            "age_min_years",
            "age_max_years",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input-text"}),
            "scope": forms.Select(attrs={"class": "input-select"}),
            "document_type": forms.TextInput(attrs={"class": "input-text"}),
            "keywords_mode": forms.Select(attrs={"class": "input-select"}),
            "experience_min_years": forms.NumberInput(attrs={"class": "input-text", "min": 0}),
            "experience_max_years": forms.NumberInput(attrs={"class": "input-text", "min": 0}),
            "age_min_years": forms.NumberInput(attrs={"class": "input-text", "min": 0}),
            "age_max_years": forms.NumberInput(attrs={"class": "input-text", "min": 0}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and getattr(self.instance, "keywords", None):
            self.fields["keywords_text"].initial = "; ".join(self.instance.keywords)

    def clean(self):
        cleaned = super().clean()
        exp_min = cleaned.get("experience_min_years")
        exp_max = cleaned.get("experience_max_years")
        if exp_min is not None and exp_max is not None and exp_min > exp_max:
            self.add_error("experience_max_years", "Maximo deve ser maior ou igual ao minimo.")
        age_min = cleaned.get("age_min_years")
        age_max = cleaned.get("age_max_years")
        if age_min is not None and age_max is not None and age_min > age_max:
            self.add_error("age_max_years", "Maximo deve ser maior ou igual ao minimo.")
        return cleaned

    def save(self, commit=True):
        instance = super().save(commit=False)
        raw = self.cleaned_data.get("keywords_text") or ""
        instance.keywords = _split_keywords(raw)
        if commit:
            instance.save()
        return instance
