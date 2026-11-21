from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .credential_models import CredentialState
from .portal_access import portal_member_for


@never_cache
@login_required(login_url='studio:portal_login')
@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
def change_password(request):
    # Validate against a fresh locked password hash so a concurrent admin reset
    # cannot be overwritten using a stale authenticated user object.
    with transaction.atomic():
        user = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        portal_member_for(user, allow_temporary=True)
        form = PasswordChangeForm(user, request.POST if request.method == 'POST' else None)
        if request.method == 'POST' and form.is_valid():
            form.save()
            CredentialState.objects.filter(user=user).update(required_change=False)
            update_session_auth_hash(request, user)
            return redirect('studio:portal_home')
    for field in form.fields.values():
        field.widget.attrs['class'] = 'portal-input'
    return render(request, 'portal/change_password.html', {'form': form})
