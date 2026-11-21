import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import PermissionDenied, ValidationError
from django.views.decorators.debug import sensitive_variables

from .credential_models import CredentialState
from .services import BusinessError


def username_for_name(name):
    User = get_user_model()
    name = User.normalize_username(name).strip()
    try:
        UnicodeUsernameValidator()(name)
    except ValidationError:
        raise BusinessError('姓名不能包含空格或特殊符号；可使用汉字、字母、数字及 @ . + - _。') from None
    limit = User._meta.get_field(User.USERNAME_FIELD).max_length
    if not name or len(name) > min(limit, 80):
        raise BusinessError('请填写有效姓名（1～80字）。')
    candidate, number = name, 2
    while User.objects.filter(username=candidate).exists():
        candidate = name[:limit-len(str(number))] + str(number)
        number += 1
    return name, candidate


@sensitive_variables('password')
def temporary_password(user):
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    # Keep the configured Django validators, including site-specific validators.
    for _ in range(100):
        password = ''.join(secrets.choice(alphabet) for _ in range(10))
        try:
            validate_password(password, user)
        except ValidationError:
            continue
        user.set_password(password)
        return password
    raise BusinessError('当前密码规则不支持生成临时密码，请联系管理员核对。')


def require_password_change(user):
    CredentialState.objects.update_or_create(user_id=user.pk, defaults={'required_change': True})


def password_change_required(user):
    return user.is_authenticated and CredentialState.objects.filter(user_id=user.pk, required_change=True).exists()


def authorize_reset(actor):
    if not (actor.is_active and actor.is_staff and actor.has_perm('studio.reset_member_credentials')):
        raise PermissionDenied
