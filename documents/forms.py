from django import forms


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
