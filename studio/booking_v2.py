"""Direct member booking; all writes reuse the existing IMMEDIATE transaction core."""
from datetime import datetime, timedelta, timezone as datetime_timezone
from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from .models import Coach, CourseType, Booking, MemberRuleConfirmation
from .services import BusinessError, _create_booking_core, _reschedule_booking_core, _request_id, _fingerprint
from .portal_services import member_operation, _owned, _rule, _duplicate, member_locked


def configuration():
    def positive(name):
        value=getattr(settings,name,None)
        try:
            if isinstance(value,bool) or str(int(value))!=str(value).strip() or int(value)<1:raise ValueError
            return int(value)
        except (TypeError,ValueError):raise BusinessError('自主预约配置不完整，请联系工作室。') from None
    days=positive('FITSTUDIO_BOOKING_MAX_DAYS')
    coach=Coach.objects.filter(pk=positive('FITSTUDIO_BOOKING_COACH_ID'),status='active').first()
    course=CourseType.objects.filter(pk=positive('FITSTUDIO_BOOKING_COURSE_TYPE_ID'),active=True,duration_minutes=60,credits_cost=1).first()
    if not coach or not course:raise BusinessError('预约教练或标准私教课程不可用，请联系工作室。')
    return coach,course,days


def validate_time(starts_at,duration=60):
    _,_,days=configuration()
    if not isinstance(starts_at,datetime) or timezone.is_naive(starts_at):raise BusinessError('请选择有效日期和时间。')
    now=timezone.now();local=timezone.localtime(starts_at);today=timezone.localtime(now).date()
    if starts_at<=now:raise BusinessError('不能预约过去或已经开始的时间。')
    if local.date()>today+timedelta(days=days):raise BusinessError(f'最多可提前{days}天预约。')
    if local.weekday()==6:raise BusinessError('星期日休息，请选择周一至周六。')
    end=local+timedelta(minutes=duration)
    if (local.second or local.microsecond or local.minute not in (0,30) or local.hour<10 or
        local.hour>21 or (local.hour==21 and local.minute!=0) or end.date()!=local.date() or
        end.hour>22 or (end.hour==22 and (end.minute or end.second))):
        raise BusinessError('请选择10:00至21:00的整点或半点，课程必须在22:00前结束。')
    return starts_at+timedelta(minutes=duration)


def free_times(member,day,*,booking=None):
    coach,course,_=configuration()
    if booking and (booking.coach_id!=coach.pk or booking.course_type_id!=course.pk):
        raise BusinessError('该历史课程请联系教练调整。')
    duration=int((booking.ends_at-booking.starts_at).total_seconds()/60) if booking else 60
    results=[]
    for minute in range(600,1261,30):
        start=timezone.make_aware(datetime.combine(day,datetime.min.time()))+timedelta(minutes=minute)
        try:end=validate_time(start,duration)
        except BusinessError:continue
        conflicts=Booking.objects.filter(status__in=['pending','completed'],starts_at__lt=end,ends_at__gt=start).filter(Q(coach=coach)|Q(member=member))
        if booking:conflicts=conflicts.exclude(pk=booking.pk)
        if not conflicts.exists() and (not booking or start!=booking.starts_at):results.append((timezone.localtime(start).strftime('%H:%M'),timezone.localtime(end).strftime('%H:%M')))
    return results


@member_operation
def book_direct(*,actor,member,requested_at,starts_at,request_id):
    if not isinstance(starts_at,datetime) or timezone.is_naive(starts_at):raise BusinessError('请选择有效日期和时间。')
    key=_request_id(request_id)
    fingerprint=_fingerprint(dict(mode='member-direct-v2',member=member.pk,start=starts_at.astimezone(datetime_timezone.utc).isoformat()))
    existing=Booking.objects.filter(request_id=key).first()
    if existing:
        if existing.member_id!=member.pk or existing.fingerprint!=fingerprint:raise BusinessError('该请求编号已用于其他预约。')
        return existing
    coach,course,_=configuration();validate_time(starts_at)
    booking=_create_booking_core(member_id=member.pk,coach_id=coach.pk,course_type_id=course.pk,starts_at=starts_at,duration_minutes=60,actor=actor,request_id=key)
    booking.fingerprint=fingerprint;booking.course_name='私教课';booking.save(update_fields=['fingerprint','course_name'])
    return booking


@member_operation
def reschedule_direct(booking_id,*,actor,member,requested_at,starts_at,request_id,confirmed,rule_version,expected_starts_at):
    booking=_owned(booking_id,member);rule=_rule('reschedule',confirmed,rule_version);key=_request_id(request_id)
    old=_duplicate(key,actor=actor,booking=booking,action='reschedule',version=rule_version)
    if old:
        if not old.result_booking or old.result_booking.starts_at!=starts_at:raise BusinessError('该请求编号已用于其他改期。')
        return old.result_booking
    if member_locked(booking):raise BusinessError('该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
    if booking.status!='pending' or booking.starts_at<=timezone.now() or booking.credits_cost!=1:raise BusinessError('该预约不可自主改期，请联系教练。')
    try: expected=datetime.fromisoformat(expected_starts_at)
    except (ValueError,TypeError):raise BusinessError('预约时间无效，请重新查看并确认。') from None
    if expected!=booking.starts_at:raise BusinessError('预约时间已变化，请重新查看并确认。')
    coach,course,_=configuration()
    if booking.coach_id!=coach.pk or booking.course_type_id!=course.pk:raise BusinessError('该历史课程请联系教练调整。')
    if starts_at==booking.starts_at:raise BusinessError('请选择与原预约不同的开始时间。')
    duration=int((booking.ends_at-booking.starts_at).total_seconds()/60);validate_time(starts_at,duration)
    replacement=_reschedule_booking_core(booking.pk,starts_at=starts_at,duration_minutes=duration,actor=actor,reason='会员自主改期',request_id=key)
    MemberRuleConfirmation.objects.create(user=actor,member=member,booking=booking,result_booking=replacement,action='reschedule',rule_version=rule_version,rule_text=rule['text'],confirmed_at=requested_at,request_id=key)
    return replacement


def enabled():
    """An explicit horizon activates V2 for legacy UI/submit compatibility too."""
    return getattr(settings,'FITSTUDIO_BOOKING_MAX_DAYS','') not in ('',None)


def validate_legacy_slot(slot,*,booking=None):
    if not enabled():return
    coach,course,_=configuration()
    duration=int((booking.ends_at-booking.starts_at).total_seconds()/60) if booking else 60
    if slot.coach_id!=coach.pk or slot.course_type_id!=course.pk or slot.duration_minutes!=duration:
        raise BusinessError('请通过自主预约入口选择新的时间。')
    validate_time(slot.starts_at,duration)
