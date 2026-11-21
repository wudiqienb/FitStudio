"""Explicit, additive daily-coach authorization. Never replaces user permissions/groups."""
from django.contrib.auth.models import Group,Permission
from django.core.exceptions import ValidationError
from django.db import transaction

DAILY_COACH_PERMISSIONS = (
    'view_member','add_member','change_member','view_coach','view_booking','add_booking',
    'change_booking','complete_booking','no_show_booking','coach_cancel_booking','makeup_booking',
    'add_purchase','view_purchase','view_lessonentry','late_cancel_booking','restore_booking','archive_member','quick_open_member','quick_renew_member',
)

@transaction.atomic
def grant_daily_coach(user):
    if not user.is_active or not user.is_staff or user.is_superuser:
        raise ValidationError('请选择有效、非超级用户的工作人员账号。')
    permissions=list(Permission.objects.filter(content_type__app_label='studio',codename__in=DAILY_COACH_PERMISSIONS))
    if len(permissions)!=len(DAILY_COACH_PERMISSIONS):
        raise ValidationError('权限记录不完整，请先在目标环境完成迁移。')
    group,_=Group.objects.get_or_create(name='教练日常业务')
    if group.permissions.exclude(content_type__app_label='studio',codename__in=DAILY_COACH_PERMISSIONS).exists():
        raise ValidationError('同名日常业务组存在额外权限，请先由管理员审查。')
    group.permissions.add(*permissions)
    user.groups.add(group)
    return group
