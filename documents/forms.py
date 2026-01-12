from django import forms

from .extractors import FIELD_CHOICES

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
        choices=FIELD_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Campos opcionais",
    )
