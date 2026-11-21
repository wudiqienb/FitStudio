from django.shortcuts import redirect
from django.urls import reverse
from django.utils.cache import add_never_cache_headers

from .credential_services import password_change_required


class MemberCredentialMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info.startswith('/portal/') and request.user.is_authenticated:
            exempt = {reverse('studio:portal_login'), reverse('studio:portal_logout'), reverse('studio:portal_password_change')}
            if request.path_info not in exempt and password_change_required(request.user):
                response = redirect('studio:portal_password_change')
                add_never_cache_headers(response)
                return response
        return self.get_response(request)
