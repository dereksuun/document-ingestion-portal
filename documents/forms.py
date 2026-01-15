from django import forms


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
