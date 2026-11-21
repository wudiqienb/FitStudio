"""管理员归档入口；保留账号绑定、课时及所有业务历史。"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import Booking, CourseType, LessonEntry, Member, MemberAccount, MemberArchiveEvent, Purchase
from .services import BusinessError


def can_archive_member(user):
    return bool(user and user.is_authenticated and user.is_active and user.is_staff and
                (user.is_superuser or user.has_perm('studio.archive_member')))


def require_archive_permission(actor):
    if not can_archive_member(actor):
        raise PermissionDenied


def reject_archived_member(member):
    if member.status == Member.Status.ARCHIVED:
        raise BusinessError('会员已归档，请由管理员恢复后再操作。')


def booking_attention(member_id):
    return Booking.objects.filter(member_id=member_id).filter(
        Q(status=Booking.Status.PENDING) | Q(starts_at__gte=timezone.now())).order_by('starts_at', 'pk')


def archive_summary(member):
    entries = LessonEntry.objects.filter(member=member).values('course_type_id').annotate(balance=Sum('delta'))
    balances = {row['course_type_id']: row['balance'] for row in entries}
    courses = CourseType.objects.filter(Q(pk__in=balances) | Q(purchase__member=member) |
                                       Q(booking__member=member)).distinct()
    return dict(balances=[{'course': course, 'balance': balances.get(course.pk, 0)} for course in courses],
                purchase_count=Purchase.objects.filter(member=member).count(),
                attention_bookings=booking_attention(member.pk))


def _transition(*, member_id, actor, reason, restore, acknowledge_bookings=False):
    require_archive_permission(actor)
    reason = (reason or '').strip()
    if len(reason) > 200:
        raise BusinessError('操作原因最多填写200字。')
    try:
        with transaction.atomic():
            actor = get_user_model().objects.get(pk=actor.pk)
            require_archive_permission(actor)
            member = Member.objects.select_for_update().get(pk=member_id)
            latest = member.archive_events.first()
            if restore and member.status != Member.Status.ARCHIVED:
                return latest if latest and latest.action == 'restore' else None
            if not restore and member.status == Member.Status.ARCHIVED:
                return latest
            if not restore and booking_attention(member.pk).exists() and not acknowledge_bookings:
                raise BusinessError('该会员仍有待处理或未来预约，请勾选确认已核对；归档不会自动取消预约。')
            binding = MemberAccount.objects.select_related('user').filter(member=member).first()
            user = binding.user if binding else None
            previous_active = user.is_active if user else None
            previous_status = member.status
            if restore:
                if not latest or latest.action != 'archive':
                    raise BusinessError('缺少原归档记录，无法安全恢复，请核对数据。')
                if latest.account_user_id != (user.pk if user else None):
                    raise BusinessError('账号绑定与归档时不一致，无法安全恢复，请核对数据。')
                member.status = Member.Status.ACTIVE
                if user:
                    user.is_active = latest.previous_user_active is True
            else:
                member.status = Member.Status.ARCHIVED
                if user:
                    user.is_active = False
            member.save(update_fields=['status', 'updated_at'])
            if user:
                user.save(update_fields=['is_active'])
            return MemberArchiveEvent.objects.create(member=member,
                action='restore' if restore else 'archive', reason=reason,
                previous_status=previous_status, account_user=user,
                previous_user_active=previous_active, created_by=actor)
    except OperationalError as error:
        if 'locked' in str(error).lower() or 'busy' in str(error).lower():
            raise BusinessError('系统正在处理其他操作，请稍后重试；本次操作未提交。') from error
        raise


def archive_member(*, member_id, actor, reason="", acknowledge_bookings=False):
    return _transition(member_id=member_id, actor=actor, reason=reason,
                       acknowledge_bookings=acknowledge_bookings, restore=False)


def restore_member(*, member_id, actor, reason=""):
    return _transition(member_id=member_id, actor=actor, reason=reason, restore=True)
