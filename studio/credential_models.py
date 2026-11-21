from django.conf import settings
from django.db import models


class CredentialState(models.Model):
    """Only the required-change flag is persisted, never the temporary secret."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    required_change = models.BooleanField(default=True)

    class Meta:
        default_permissions = ()
        permissions = [('reset_member_credentials', '重置会员临时密码')]
