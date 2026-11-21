"""会员本人操作：身份及规则校验与原课务核心在同一 SQLite 写事务中。"""
from functools import wraps
from datetime import timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core import signing
from django.db import OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import AvailableSlot, Booking, MemberRuleConfirmation
from .portal_access import portal_member_for
from .portal_rules import LOCKED_MESSAGE, RULES
from .services import (BusinessError, _create_booking_core, _reschedule_booking_core,
                       _cancel_booking_core, _request_id)


def member_operation(fn):
    @wraps(fn)
    def wrapped(*args, actor, **kwargs):
        requested_at = timezone.now()
        try:
            with transaction.atomic():
                try:
                    current_user = get_user_model().objects.get(pk=actor.pk)
                except (AttributeError, get_user_model().DoesNotExist):
                    raise PermissionDenied from None
                member = portal_member_for(current_user)
                return fn(*args, actor=current_user, member=member, requested_at=requested_at, **kwargs)
        except OperationalError as error:
            if 'locked' in str(error).lower() or 'busy' in str(error).lower():
                raise BusinessError('系统正在处理其他操作，请稍后重试；本次操作未提交。') from error
            raise
    return wrapped


def _owned(booking_id, member):
    booking = Booking.objects.filter(pk=booking_id, member=member).select_related('course_type', 'coach').first()
    if booking is None:
        raise PermissionDenied
    return booking


def _slot(slot_id, now):
    slot = AvailableSlot.objects.select_related('course_type', 'coach').filter(pk=slot_id).first()
    if (not slot or not slot.active or not slot.course_type.active or slot.course_type.credits_cost != 1
            or slot.coach.status != 'active' or slot.starts_at <= now):
        raise BusinessError('该时段不可预约，请选择其他时间。')
    return slot


def _slot_fingerprint(slot):
    return [slot.pk, slot.course_type_id, slot.coach_id,
            slot.starts_at.astimezone(datetime_timezone.utc).isoformat(), slot.duration_minutes]


def slot_stamp(slot):
    return signing.dumps(_slot_fingerprint(slot), salt='studio.member-slot')


def _check_slot_stamp(slot, stamp):
    try:
        original = signing.loads(stamp, salt='studio.member-slot')
    except (signing.BadSignature, TypeError):
        raise BusinessError('时段信息无效，请重新打开页面。') from None
    current = _slot_fingerprint(slot)
    if original != current:
        raise BusinessError('该时段已变更，请重新查看并选择。')


def slot_open(slot, now=None, exclude_booking=None):
    now = now or timezone.now()
    if (not slot.active or not slot.course_type.active or slot.course_type.credits_cost != 1
            or slot.coach.status != 'active' or slot.starts_at <= now):
        return False
    end = slot.starts_at + timedelta(minutes=slot.duration_minutes)
    occupied = Booking.objects.filter(coach=slot.coach, status__in=['pending', 'completed'],
                                      starts_at__lt=end, ends_at__gt=slot.starts_at)
    if exclude_booking:
        occupied = occupied.exclude(pk=exclude_booking)
    return not occupied.exists()


def _rule(action, confirmed, version):
    rule = RULES[action]
    if confirmed is not True or version != rule['version']:
        raise BusinessError('请阅读并勾选确认当前规则后再提交。')
    return rule


def _duplicate(key, *, actor, booking, action, version):
    existing = MemberRuleConfirmation.objects.filter(request_id=key).select_related('result_booking').first()
    if existing and (existing.user_id != actor.pk or existing.booking_id != booking.pk or
                     existing.action != action or existing.rule_version != version):
        raise BusinessError('该请求编号已使用，请重新打开页面。')
    return existing


def member_locked(booking):
    if booking.status == Booking.Status.RESCHEDULED:
        return True
    # Staff can make further replacements; carry forward a previous member lock.
    current = booking
    seen = set()
    while current and current.pk not in seen:
        seen.add(current.pk)
        if MemberRuleConfirmation.objects.filter(action='reschedule', result_booking_id=current.pk).exists():
            return True
        current = current.previous_booking if current.previous_booking_id else None
    return False


@member_operation
def member_book(*, actor, member, requested_at, slot_id, request_id, expected_slot):
    key = _request_id(request_id)
    existing = Booking.objects.filter(request_id=key).first()
    if existing:
        if existing.member_id != member.pk or existing.slot_id != int(slot_id):
            raise BusinessError('该请求编号已使用，请重新打开页面。')
        return existing
    slot = _slot(slot_id, timezone.now())
    _check_slot_stamp(slot, expected_slot)
    from .booking_v2 import validate_legacy_slot
    validate_legacy_slot(slot)
    if not slot_open(slot):
        raise BusinessError('该时段已满或不可预约，请选择其他时间。')
    booking = _create_booking_core(member_id=member.pk, coach_id=slot.coach_id,
        course_type_id=slot.course_type_id, starts_at=slot.starts_at, duration_minutes=slot.duration_minutes,
        actor=actor, request_id=key)
    booking.slot = slot
    booking.save(update_fields=['slot'])
    return booking


@member_operation
def member_cancel(booking_id, *, actor, member, requested_at, confirmed, rule_version, request_id,
                  expected_outcome, expected_starts_at):
    booking = _owned(booking_id, member)
    rule = _rule('cancel', confirmed, rule_version)
    key = _request_id(request_id)
    old = _duplicate(key, actor=actor, booking=booking, action='cancel', version=rule_version)
    if old:
        return booking
    if member_locked(booking):
        raise BusinessError(LOCKED_MESSAGE)
    if booking.status != Booking.Status.PENDING:
        raise BusinessError('只有待上课的预约可以取消。')
    if booking.credits_cost != 1:
        raise BusinessError('该预约请联系教练调整。')
    if expected_outcome not in ('normal', 'late') or not expected_starts_at:
        raise BusinessError('请重新查看取消规则并确认。')
    result = _cancel_booking_core(booking.pk, actor=actor, reason='会员自主取消',
        expected_outcome=expected_outcome, expected_starts_at=expected_starts_at,
        _server_requested_at=requested_at, member_late_authorized=True)
    MemberRuleConfirmation.objects.create(user=actor, member=member, booking=booking,
        action='cancel', rule_version=rule_version, rule_text=rule['text'],
        confirmed_at=requested_at, request_id=key)
    return result


@member_operation
def member_reschedule(booking_id, *, actor, member, requested_at, confirmed, rule_version, request_id,
                      expected_starts_at, slot_id, expected_slot):
    booking = _owned(booking_id, member)
    rule = _rule('reschedule', confirmed, rule_version)
    key = _request_id(request_id)
    old = _duplicate(key, actor=actor, booking=booking, action='reschedule', version=rule_version)
    if old:
        if not old.result_booking or old.result_booking.slot_id != int(slot_id):
            raise BusinessError('该请求编号已使用，请重新打开页面。')
        return old.result_booking
    if member_locked(booking):
        raise BusinessError(LOCKED_MESSAGE)
    if (booking.status != Booking.Status.PENDING or booking.starts_at <= timezone.now()
            or booking.credits_cost != 1):
        raise BusinessError('该课程不可自主改期，请联系教练。')
    if not expected_starts_at or expected_starts_at != booking.starts_at.isoformat():
        raise BusinessError('预约时间已变化，请重新查看并确认。')
    slot = _slot(slot_id, timezone.now())
    _check_slot_stamp(slot, expected_slot)
    from .booking_v2 import validate_legacy_slot
    validate_legacy_slot(slot, booking=booking)
    if slot.coach_id != booking.coach_id or slot.course_type_id != booking.course_type_id:
        raise BusinessError('请选择原课程和原教练的可预约时间。')
    if slot.starts_at == booking.starts_at:
        raise BusinessError('请选择与原预约不同的开始时间。')
    if not slot_open(slot, exclude_booking=booking.pk):
        raise BusinessError('该时段已满或不可预约，请选择其他时间。')
    replacement = _reschedule_booking_core(booking.pk, starts_at=slot.starts_at,
        duration_minutes=slot.duration_minutes, actor=actor, reason='会员自主改期', request_id=key)
    replacement.slot = slot
    replacement.save(update_fields=['slot'])
    MemberRuleConfirmation.objects.create(user=actor, member=member, booking=booking,
        result_booking=replacement, action='reschedule', rule_version=rule_version,
        rule_text=rule['text'], confirmed_at=requested_at, request_id=key)
    return replacement
