from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied

from .portal_access import portal_member_for


class MemberAuthenticationForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "账号"
        self.fields["username"].widget.attrs.update({
            "class": "portal-input", "autocomplete": "username",
            "autocapitalize": "none", "spellcheck": "false",
            "placeholder": "请输入会员账号",
        })
        self.fields["password"].widget.attrs.update({
            "class": "portal-input", "autocomplete": "current-password",
            "placeholder": "请输入密码",
        })

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        try:
            portal_member_for(user, allow_temporary=True)
        except PermissionDenied:
            raise forms.ValidationError(
                "当前账号无法进入会员中心，请联系工作室核实账号开通情况。",
                code="member_access_denied",
            ) from None
