"""Presentation-only member responses; business operations remain in existing services."""
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils import timezone
from .models import Booking, LessonEntry
from .portal_services import member_locked
from .services import credit_snapshot


def enhanced(request):
    return request.headers.get('X-FitStudio-Interaction') == '1'


def account_for(member_id, course_id):
    account = credit_snapshot(member_id, course_id)
    return dict(account, held=account['balance'] - account['available'])


def card_context(booking):
    locked = member_locked(booking)
    return {'booking': booking, 'locked': locked,
            'can_change': booking.status == Booking.Status.PENDING and booking.starts_at > timezone.now() and booking.credits_cost == 1 and not locked}


def outcome_text(outcome):
    return {'normal': '本次取消不扣课时，释放预约占用。', 'late': '本次取消将扣除1课时。', 'started': '课程已开始，无法自主取消。'}[outcome]


def failure(message, form=None, **extra):
    return JsonResponse({'ok': False, 'message': message, 'reconfirm': True,
                         'errors': form.errors.get_json_data() if form else {}, **extra}, status=422)


def success(request, booking, action):
    booking.refresh_from_db()
    account = account_for(booking.member_id, booking.course_type_id)
    context = card_context(booking)
    if action == 'book':
        message = f'预约成功，已占用{booking.credits_cost}节可用课时，实际余额未扣除。' if booking.status == Booking.Status.PENDING else f'预约请求已处理，当前状态：{booking.get_status_display()}。'
    elif action == 'cancel':
        if booking.status == Booking.Status.LATE_CANCELLED:
            entry = LessonEntry.objects.get(booking=booking, kind=LessonEntry.Kind.LATE_CANCEL)
            message = f'迟取消成功，已扣除{abs(entry.delta)}节课时，预约占用已解除。'
        else:
            message = f'取消成功，不扣课，已释放{booking.credits_cost}节预约占用课时。'
    else:
        message = '改期成功，新预约已锁定，不能再次改期或取消。'
    context.update(account=account, action=action)
    html = render_to_string('portal/interaction_result.html', context, request=request)
    return JsonResponse({'ok': True, 'message': message, 'html': html, 'account': account,
                         'locked': context['locked'], 'booking_id': booking.pk})
