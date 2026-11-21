from django.core.exceptions import PermissionDenied

from .models import Member, MemberAccount


def portal_member_for(user, *, allow_temporary=False):
    """每次请求从数据库重新确认会员身份，不信任客户端编号或会话快照。"""
    if (not user.is_authenticated or not user.is_active or
            user.is_staff or user.is_superuser):
        raise PermissionDenied
    binding = MemberAccount.objects.select_related("member").filter(
        user_id=user.pk, member__status=Member.Status.ACTIVE).first()
    if binding is None:
        raise PermissionDenied
    from .credential_services import password_change_required
    if not allow_temporary and password_change_required(user):
        raise PermissionDenied
    return binding.member
