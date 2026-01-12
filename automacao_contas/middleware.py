from django.conf import settings
from django.shortcuts import redirect


class LoginRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.login_url = settings.LOGIN_URL
        self.allowed_prefixes = [
            self.login_url,
            "/logout/",
            "/admin/",
        ]
        if settings.STATIC_URL:
            self.allowed_prefixes.append(settings.STATIC_URL)
        if settings.MEDIA_URL:
            self.allowed_prefixes.append(settings.MEDIA_URL)

    def __call__(self, request):
        if request.user.is_authenticated:
            return self.get_response(request)

        if self._is_allowed_path(request.path):
            return self.get_response(request)

        return redirect(f"{self.login_url}?next={request.get_full_path()}")

    def _is_allowed_path(self, path):
        for prefix in self.allowed_prefixes:
            if path.startswith(prefix):
                return True
        return False
