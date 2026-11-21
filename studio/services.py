"""课时唯一写入入口：授权 → IMMEDIATE 事务 → 校验 → 同时提交业务与流水。"""
import hashlib
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.db.models import IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Booking, BookingEvent, Coach, CourseType, LessonAdjustment, LessonEntry, Member, Package, Purchase


class BusinessError(ValidationError):
    """可以显示给操作员的业务校验错误。"""


def _authorize(actor, permission):
    if not actor or not actor.is_active or not actor.is_staff or not actor.has_perm("studio." + permission):
        raise PermissionDenied


def operation(permission, capture_request_time=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, actor, **kwargs):
            _authorize(actor, permission)
            if capture_request_time:
                # 由服务端在取得SQLite写锁之前取时；忽略调用方传入的同名值。
                kwargs["_server_requested_at"] = timezone.now()
            try:
                with transaction.atomic():
                    return function(*args, actor=actor, **kwargs)
            except OperationalError as error:
                if "locked" in str(error).lower() or "busy" in str(error).lower():
                    raise BusinessError("系统正在处理其他操作，请稍后重试；本次操作未提交。") from error
                raise
        return wrapped
    return decorate


def credit_balance(member_id, course_type_id):
    return LessonEntry.objects.filter(member_id=member_id, course_type_id=course_type_id).aggregate(total=Sum("delta"))["total"] or 0


def credit_snapshot(member_id, course_type_id, exclude_booking=None):
    """一个 SELECT 同时读取余额和占用，避免页面跨提交读到混合时刻。"""
    credits = LessonEntry.objects.filter(member_id=member_id, course_type_id=OuterRef("pk")).order_by().values(
        "course_type_id").annotate(total=Sum("delta")).values("total")
    pending = Booking.objects.filter(member_id=member_id, course_type_id=OuterRef("pk"), status=Booking.Status.PENDING)
    if exclude_booking:
        pending = pending.exclude(pk=exclude_booking)
    reserved = pending.order_by().values("course_type_id").annotate(total=Sum("credits_cost")).values("total")
    row = CourseType.objects.filter(pk=course_type_id).annotate(
        ledger_balance=Coalesce(Subquery(credits), Value(0), output_field=IntegerField()),
        held_credits=Coalesce(Subquery(reserved), Value(0), output_field=IntegerField())
    ).values("ledger_balance", "held_credits").first()
    return {"balance": row["ledger_balance"], "available": row["ledger_balance"]-row["held_credits"]} if row else {"balance": 0, "available": 0}


def available_credits(member_id, course_type_id, exclude_booking=None):
    return credit_snapshot(member_id, course_type_id, exclude_booking)["available"]


def _reason(reason):
    reason = (reason or "").strip()
    if not reason or len(reason) > 200:
        raise BusinessError("请填写原因（1～200字）。")
    return reason


def _fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _request_id(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise BusinessError("请求编号无效，请重新打开表单。") from error


def _get(model, pk):
    try:
        return model.objects.get(pk=pk)
    except model.DoesNotExist as error:
        raise BusinessError("关联档案不存在，请刷新后重试。") from error


def _validate(obj):
    try:
        obj.full_clean()
    except ValidationError as error:
        raise BusinessError(error.messages) from error


def purchase_operation(function):
    @wraps(function)
    def wrapped(*args, actor, **kwargs):
        if kwargs.get('quick_order_id') is None:
            _authorize(actor, 'add_purchase')
        else:
            from .quick_services import authorize_quick_purchase
            authorize_quick_purchase(actor, kwargs)
        try:
            with transaction.atomic():
                return function(*args, actor=actor, **kwargs)
        except OperationalError as error:
            if 'locked' in str(error).lower() or 'busy' in str(error).lower():
                raise BusinessError('系统正在处理其他操作，请稍后重试；本次操作未提交。') from error
            raise
    return wrapped


@purchase_operation
def register_purchase(*, member_id, package_id, purchased_on, actor, request_id, reason, credits=None, amount=None, occurred_at=None, quick_order_id=None):
    reason = _reason(reason)
    key = _request_id(request_id)
    fingerprint_data = dict(member=member_id, package=package_id, date=purchased_on, reason=reason,
                            credits=credits, amount=None if amount is None else str(amount))
    if occurred_at is not None:
        if not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at) or timezone.localtime(occurred_at, timezone.get_default_timezone()).date() != purchased_on:
            raise BusinessError("实际购买时间必须与购买日期一致。")
        fingerprint_data["occurred_at"] = occurred_at.isoformat()
    fingerprint = _fingerprint(fingerprint_data)
    existing = Purchase.objects.filter(request_id=key).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise BusinessError("同一请求编号不能用于不同的购课内容。")
        return existing
    member, package = _get(Member, member_id), _get(Package, package_id)
    if member.status != Member.Status.ACTIVE or not package.active or not package.course_type.active:
        raise BusinessError("会员、套餐及课程类型必须为启用状态。")
    credits = package.credits if credits is None else credits
    amount = package.price if amount is None else amount
    try:
        amount = Decimal(str(amount))
        valid_amount = amount.is_finite() and amount >= 0 and amount == amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        valid_amount = False
    if type(credits) is not int or credits < 1 or not valid_amount or not isinstance(purchased_on, date):
        raise BusinessError("课时必须为正整数，金额须为非负且最多两位小数，购买日期必须有效。")
    purchase = Purchase(member=member, package=package, course_type=package.course_type,
        package_name=package.name, credits=credits, amount=amount, purchased_on=purchased_on,
        reason=reason, request_id=key, fingerprint=fingerprint, created_by=actor)
    _validate(purchase)
    before = credit_balance(member.pk, package.course_type_id)
    purchase.save()
    LessonEntry.objects.create(member=member, course_type=package.course_type, kind=LessonEntry.Kind.PURCHASE,
        delta=credits, balance_before=before, balance_after=before + credits,
        purchase=purchase, reason=reason, created_by=actor, occurred_at=occurred_at)
    return purchase


def _times(starts_at, duration_minutes):
    if (not isinstance(starts_at, datetime) or timezone.is_naive(starts_at) or
        type(duration_minutes) is not int or not 15 <= duration_minutes <= 240):
        raise BusinessError("请选择有效的日期、时间，以及15～240分钟的整数时长。")
    return starts_at + timedelta(minutes=duration_minutes)


def _check_booking(member, coach, course, starts_at, ends_at, cost, exclude=None):
    if member.status != Member.Status.ACTIVE or coach.status != Coach.Status.ACTIVE or not course.active:
        raise BusinessError("请选择正常会员、在职教练和启用的课程。")
    conflicts = Booking.objects.filter(status__in=[Booking.Status.PENDING, Booking.Status.COMPLETED],
        starts_at__lt=ends_at, ends_at__gt=starts_at).filter(Q(coach=coach) | Q(member=member))
    if exclude:
        conflicts = conflicts.exclude(pk=exclude)
    if conflicts.exists():
        raise BusinessError("该时段教练或会员已有课程，请选择其他时间。")
    if available_credits(member.pk, course.pk, exclude_booking=exclude) < cost:
        raise BusinessError("该课程可用课时不足，请先购课或取消其他待上课预约。")


def _event(booking, action, actor, reason, *, occurred_at=None, **details):
    BookingEvent.objects.create(booking=booking, action=action, created_by=actor, reason=reason,
                                occurred_at=occurred_at or timezone.now(), details=details)


def cancel_outcome(booking, requested_at=None):
    """只以工作室时区计算，调用方给出的时间仅供内部预览/测试。"""
    now = requested_at or timezone.now()
    if timezone.is_naive(now):
        raise BusinessError("取消时间必须包含时区。")
    if now >= booking.starts_at:
        return "started"
    zone = timezone.get_default_timezone()
    today = timezone.localtime(now, zone).date()
    lesson_day = timezone.localtime(booking.starts_at, zone).date()
    return "normal" if today < lesson_day else "late"


def _consume(booking, kind, actor, reason, occurred_at):
    """已处于写事务中：保护其他待上课预约的占用，写一条负数流水。"""
    before = credit_balance(booking.member_id, booking.course_type_id)
    own_available = available_credits(booking.member_id, booking.course_type_id, exclude_booking=booking.pk)
    if before < booking.credits_cost or own_available < booking.credits_cost:
        raise BusinessError("课时余额不足或已被其他预约占用，请先核对课时记录。")
    LessonEntry.objects.create(member=booking.member, course_type=booking.course_type,
        booking=booking, kind=kind, delta=-booking.credits_cost,
        balance_before=before, balance_after=before-booking.credits_cost,
        created_by=actor, reason=reason, occurred_at=occurred_at)


def _create_booking_core(*, member_id, coach_id, course_type_id, starts_at, duration_minutes, actor, request_id):
    course = _get(CourseType, course_type_id)
    ends_at = _times(starts_at, course.duration_minutes if duration_minutes is None else duration_minutes)
    key = _request_id(request_id)
    fingerprint = _fingerprint(dict(member=member_id, coach=coach_id, course=course_type_id,
                                    start=starts_at.isoformat(), duration=duration_minutes))
    existing = Booking.objects.filter(request_id=key).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise BusinessError("同一请求编号不能用于不同的预约内容。")
        return existing
    member, coach = _get(Member, member_id), _get(Coach, coach_id)
    _check_booking(member, coach, course, starts_at, ends_at, course.credits_cost)
    booking = Booking(member=member, coach=coach, course_type=course, course_name=course.name,
        starts_at=starts_at, ends_at=ends_at, credits_cost=course.credits_cost, created_by=actor,
        request_id=key, fingerprint=fingerprint)
    _validate(booking)
    booking.save()
    _event(booking, "create", actor, "创建预约，不扣课", starts_at=starts_at.isoformat(), ends_at=ends_at.isoformat())
    return booking


@operation("add_booking")
def create_booking(*, member_id, coach_id, course_type_id, starts_at, duration_minutes, actor, request_id):
    return _create_booking_core(member_id=member_id, coach_id=coach_id, course_type_id=course_type_id,
        starts_at=starts_at, duration_minutes=duration_minutes, actor=actor, request_id=request_id)


def _replacement_key(booking_id, kind, starts_at, duration_minutes, coach_id, reason, request_id):
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise BusinessError("请选择有效的日期和时间。")
    payload = dict(original=booking_id, kind=kind, starts_at=starts_at.isoformat(),
                   duration=duration_minutes, coach=coach_id, reason=reason)
    fingerprint = _fingerprint(payload)
    key = _request_id(request_id) if request_id is not None else uuid.uuid5(uuid.NAMESPACE_URL, fingerprint)
    return key, fingerprint


def _existing_replacement(key, fingerprint, original_id):
    existing = Booking.objects.filter(request_id=key).first()
    if existing and (existing.fingerprint != fingerprint or existing.previous_booking_id != original_id):
        raise BusinessError("同一请求编号不能用于不同的预约内容。")
    return existing


def _reschedule_booking_core(booking_id, *, starts_at, duration_minutes, actor, reason, request_id=None):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    key, fingerprint = _replacement_key(booking_id, "reschedule", starts_at, duration_minutes,
                                         booking.coach_id, reason, request_id)
    existing = _existing_replacement(key, fingerprint, booking.pk)
    if existing:
        return existing
    if booking.status != Booking.Status.PENDING:
        raise BusinessError("只有待上课预约可以改期。")
    ends_at = _times(starts_at, duration_minutes)
    _check_booking(booking.member, booking.coach, booking.course_type, starts_at, ends_at,
                   booking.credits_cost, exclude=booking.pk)
    booking.status = Booking.Status.RESCHEDULED
    booking.save(update_fields=["status", "updated_at"])
    replacement = Booking(member=booking.member, coach=booking.coach, course_type=booking.course_type,
        course_name=booking.course_name, starts_at=starts_at, ends_at=ends_at, credits_cost=booking.credits_cost,
        previous_booking=booking, replacement_type="reschedule", makeup_origin=booking.makeup_origin,
        created_by=actor, request_id=key, fingerprint=fingerprint)
    _validate(replacement)
    replacement.save()
    _event(booking, "reschedule", actor, reason, before={"starts_at": booking.starts_at.isoformat(), "ends_at": booking.ends_at.isoformat()},
           after={"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}, replacement_id=replacement.pk)
    _event(replacement, "rescheduled_in", actor, reason, previous_id=booking.pk)
    return replacement


@operation("change_booking")
def reschedule_booking(booking_id, *, starts_at, duration_minutes, actor, reason, request_id=None):
    return _reschedule_booking_core(booking_id, starts_at=starts_at, duration_minutes=duration_minutes,
        actor=actor, reason=reason, request_id=request_id)


@operation("change_booking", capture_request_time=True)
def cancel_booking(booking_id, *, actor, reason, expected_outcome=None, expected_starts_at=None, _server_requested_at=None):
    return _cancel_booking_core(booking_id, actor=actor, reason=reason, expected_outcome=expected_outcome,
        expected_starts_at=expected_starts_at, _server_requested_at=_server_requested_at)


def _cancel_booking_core(booking_id, *, actor, reason, expected_outcome=None, expected_starts_at=None,
                         _server_requested_at=None, member_late_authorized=False):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    if booking.status in (Booking.Status.CANCELLED, Booking.Status.LATE_CANCELLED):
        if booking.status == Booking.Status.LATE_CANCELLED:
            if not member_late_authorized:
                _authorize(actor, "late_cancel_booking")
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BusinessError("只有待上课预约可以取消；已完成或扣课的预约需单独冲正。")
    if expected_starts_at is not None and str(expected_starts_at) != booking.starts_at.isoformat():
        raise BusinessError("预约时间已经改变，请重新查看并确认。")
    outcome = cancel_outcome(booking, _server_requested_at)
    if outcome == "started" or timezone.now() >= booking.starts_at:
        raise BusinessError("课程已经开始，请根据实际情况选择爽约、完成或教练取消。")
    if expected_outcome is not None and expected_outcome != outcome:
        raise BusinessError("取消扣课结果已变化，请重新打开页面确认。")
    if outcome == "late":
        if not member_late_authorized:
            _authorize(actor, "late_cancel_booking")
        _consume(booking, LessonEntry.Kind.LATE_CANCEL, actor, reason, _server_requested_at)
        booking.status = Booking.Status.LATE_CANCELLED
    else:
        booking.status = Booking.Status.CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "late_cancel" if outcome == "late" else "cancel", actor, reason,
           occurred_at=_server_requested_at, before="pending", after=booking.status)
    return booking


@operation("complete_booking")
def complete_booking(booking_id, *, actor, reason="确认课程完成"):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    if booking.status == Booking.Status.COMPLETED:
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BusinessError("仅待上课预约可以确认完成。")
    if booking.ends_at > timezone.now():
        raise BusinessError("课程尚未结束，不能提前确认完成。")
    _consume(booking, LessonEntry.Kind.COMPLETE, actor, reason, booking.ends_at)
    booking.status = Booking.Status.COMPLETED
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "complete", actor, reason, before="pending", after="completed")
    return booking


@operation("restore_booking")
def restore_booking(booking_id, *, actor, reason):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    if booking.status == Booking.Status.REVERSED:
        return booking
    if booking.status not in (Booking.Status.COMPLETED, Booking.Status.LATE_CANCELLED, Booking.Status.NO_SHOW):
        raise BusinessError("只有已完成、迟取消或爽约且已经扣课的预约可以冲正。")
    original = LessonEntry.objects.get(booking=booking, kind__in=[LessonEntry.Kind.COMPLETE, LessonEntry.Kind.LATE_CANCEL, LessonEntry.Kind.NO_SHOW])
    before = credit_balance(booking.member_id, booking.course_type_id)
    LessonEntry.objects.create(member=booking.member, course_type=booking.course_type,
        booking=booking, kind=LessonEntry.Kind.RESTORE, reverses=original, delta=-original.delta,
        balance_before=before, balance_after=before-original.delta, created_by=actor, reason=reason,
        occurred_at=timezone.now())
    previous_status = booking.status
    booking.status = Booking.Status.REVERSED
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "restore", actor, reason, before=previous_status, after="reversed")
    return booking


@operation("no_show_booking")
def mark_no_show(booking_id, *, actor, reason):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    if booking.status == Booking.Status.NO_SHOW:
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BusinessError("只有待上课预约可以登记爽约。")
    now = timezone.now()
    if now < booking.starts_at:
        raise BusinessError("课程尚未开始，不能登记爽约。")
    _consume(booking, LessonEntry.Kind.NO_SHOW, actor, reason, now)
    booking.status = Booking.Status.NO_SHOW
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "no_show", actor, reason, occurred_at=now, before="pending", after="no_show")
    return booking


@operation("coach_cancel_booking")
def coach_cancel_booking(booking_id, *, actor, reason):
    reason = _reason(reason)
    booking = _get(Booking, booking_id)
    if booking.status == Booking.Status.COACH_CANCELLED:
        return booking
    if booking.status != Booking.Status.PENDING:
        raise BusinessError("只有待上课预约可以登记教练取消。")
    booking.status = Booking.Status.COACH_CANCELLED
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "coach_cancel", actor, reason, before="pending", after="coach_cancelled")
    return booking


@operation("makeup_booking")
def create_makeup_booking(booking_id, *, starts_at, duration_minutes, coach_id, actor, request_id, reason):
    reason = _reason(reason)
    original = _get(Booking, booking_id)
    key, fingerprint = _replacement_key(booking_id, "makeup", starts_at, duration_minutes, coach_id, reason, request_id)
    existing = _existing_replacement(key, fingerprint, original.pk)
    if existing:
        return existing
    is_cancelled_makeup = original.status == Booking.Status.CANCELLED and original.makeup_origin_id is not None
    if original.status != Booking.Status.COACH_CANCELLED and not is_cancelled_makeup:
        raise BusinessError("只有等待安排的教练取消或正常取消的补课可以安排补课。")
    if Booking.objects.filter(previous_booking=original).exists():
        raise BusinessError("该预约已经安排了后继，请打开关联预约查看。")
    coach = _get(Coach, coach_id)
    ends_at = _times(starts_at, duration_minutes)
    _check_booking(original.member, coach, original.course_type, starts_at, ends_at, original.credits_cost)
    makeup = Booking(member=original.member, coach=coach, course_type=original.course_type,
        course_name=original.course_name, starts_at=starts_at, ends_at=ends_at, credits_cost=original.credits_cost,
        previous_booking=original, replacement_type="makeup", makeup_origin=original.makeup_origin or original,
        created_by=actor, request_id=key, fingerprint=fingerprint)
    _validate(makeup)
    makeup.save()
    _event(original, "makeup_scheduled", actor, reason, replacement_id=makeup.pk)
    _event(makeup, "makeup", actor, reason, previous_id=original.pk)
    return makeup


@operation("backfill_booking")
def record_historical_lesson(*, member_id, coach_id, course_type_id, starts_at, duration_minutes, actor, request_id, reason):
    reason = _reason(reason)
    if not isinstance(starts_at, datetime) or timezone.is_naive(starts_at):
        raise BusinessError("请选择有效的历史上课日期与时间。")
    key = _request_id(request_id)
    fingerprint = _fingerprint(dict(operation="backfill", member=member_id, coach=coach_id,
        course=course_type_id, start=starts_at.isoformat(), duration=duration_minutes, reason=reason))
    existing = Booking.objects.filter(request_id=key).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise BusinessError("同一请求编号不能用于不同的补录内容。")
        return existing
    course = _get(CourseType, course_type_id)
    ends_at = _times(starts_at, course.duration_minutes if duration_minutes is None else duration_minutes)
    if ends_at > timezone.now():
        raise BusinessError("课程尚未结束，不能作为历史课程补录。")
    member, coach = _get(Member, member_id), _get(Coach, coach_id)
    _check_booking(member, coach, course, starts_at, ends_at, course.credits_cost)
    reversed_ids = LessonEntry.objects.filter(kind=LessonEntry.Kind.RESTORE, reverses__isnull=False).values("reverses_id")
    existing_charges = LessonEntry.objects.filter(member=member, kind__in=[LessonEntry.Kind.COMPLETE,
        LessonEntry.Kind.LATE_CANCEL, LessonEntry.Kind.NO_SHOW], booking__starts_at__lt=ends_at,
        booking__ends_at__gt=starts_at).exclude(pk__in=reversed_ids)
    if existing_charges.exists():
        raise BusinessError("该会员时段已有未冲正的扣课记录，请先核对历史流水。")
    booking = Booking(member=member, coach=coach, course_type=course, course_name=course.name,
        starts_at=starts_at, ends_at=ends_at, credits_cost=course.credits_cost,
        created_by=actor, request_id=key, fingerprint=fingerprint)
    _validate(booking)
    booking.save()
    _event(booking, "backfill", actor, reason, occurred_at=ends_at,
           actual_start=starts_at.isoformat(), actual_end=ends_at.isoformat())
    _consume(booking, LessonEntry.Kind.COMPLETE, actor, reason, ends_at)
    booking.status = Booking.Status.COMPLETED
    booking.save(update_fields=["status", "updated_at"])
    _event(booking, "complete", actor, reason, occurred_at=ends_at, before="pending", after="completed")
    return booking


@operation("add_lessonadjustment")
def adjust_credits(*, member_id, course_type_id, delta, occurred_at, actor, request_id, reason):
    reason = _reason(reason)
    if type(delta) is not int or delta == 0:
        raise BusinessError("课时调整数量必须为非零整数。")
    if not isinstance(occurred_at, datetime) or timezone.is_naive(occurred_at) or occurred_at > timezone.now():
        raise BusinessError("请选择不晚于当前时间的实际发生时间。")
    key = _request_id(request_id)
    fingerprint = _fingerprint(dict(member=member_id, course=course_type_id, delta=delta,
                                    occurred_at=occurred_at.isoformat(), reason=reason))
    existing = LessonAdjustment.objects.filter(request_id=key).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise BusinessError("同一请求编号不能用于不同的调整内容。")
        return existing
    member, course = _get(Member, member_id), _get(CourseType, course_type_id)
    before = credit_balance(member.pk, course.pk)
    if delta < 0 and available_credits(member.pk, course.pk) < -delta:
        raise BusinessError("可用课时不足；调整不能侵占其他待上课预约。")
    adjustment = LessonAdjustment(member=member, course_type=course, delta=delta, occurred_at=occurred_at,
        reason=reason, request_id=key, fingerprint=fingerprint, created_by=actor)
    _validate(adjustment)
    adjustment.save()
    LessonEntry.objects.create(member=member, course_type=course, kind=LessonEntry.Kind.ADJUSTMENT,
        delta=delta, balance_before=before, balance_after=before+delta,
        adjustment=adjustment, reason=reason, created_by=actor, occurred_at=occurred_at)
    return adjustment
