from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.contrib import messages
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, reverse_lazy
from django import forms
from django.core import signing
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .portal_feedback import enhanced, success, failure, account_for, card_context, outcome_text
from .portal_access import portal_member_for
from .portal_forms import MemberAuthenticationForm
from .models import AvailableSlot, Booking, CourseType
from .portal_rules import RULES
from .portal_services import member_book, member_cancel, member_reschedule, member_locked, slot_open, slot_stamp
from .services import BusinessError, cancel_outcome, credit_snapshot
import uuid
from datetime import timedelta


class MemberLoginView(LoginView):
    template_name = "portal/login.html"
    authentication_form = MemberAuthenticationForm

    def get_success_url(self):
        # 会员入口始终返回本人首页，不接受客户端指定数据对象或外部地址。
        from .credential_services import password_change_required
        if password_change_required(self.request.user):
            return reverse('studio:portal_password_change')
        return reverse("studio:portal_home")


class MemberLogoutView(LogoutView):
    next_page = reverse_lazy("studio:portal_login")


@never_cache
@login_required(login_url="studio:portal_login")
@require_safe
def member_home(request):
    member = portal_member_for(request.user)
    accounts = [dict(course=course, **account_for(member.pk, course.pk)) for course in CourseType.objects.all()]
    upcoming = Booking.objects.filter(member=member, status=Booking.Status.PENDING,
        starts_at__gte=timezone.now()).select_related('coach').order_by('starts_at')[:5]
    from django.db.models import Count
    counts = dict(Booking.objects.filter(member=member).values_list('status').annotate(total=Count('pk')))
    stats = [{'label': label, 'status': value, 'count': counts.get(value, 0)} for value, label in Booking.Status.choices if counts.get(value, 0)]
    return render(request, "portal/home.html", {"member": member, 'accounts': accounts, 'upcoming': upcoming, 'booking_stats': stats})


class RequestForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class BookForm(RequestForm):
    expected_slot = forms.CharField(widget=forms.HiddenInput)
    confirmation_token = forms.CharField(widget=forms.HiddenInput)


class ConfirmationForm(RequestForm):
    confirmed = forms.BooleanField(label='我已阅读并同意以上规则', required=True)
    rule_version = forms.CharField(widget=forms.HiddenInput)
    expected_starts_at = forms.CharField(widget=forms.HiddenInput)


class CancelForm(ConfirmationForm):
    expected_outcome = forms.ChoiceField(choices=[('normal', '正常取消'), ('late', '迟取消')], widget=forms.HiddenInput)


class RescheduleForm(ConfirmationForm):
    slot_selection = forms.CharField(label='选择新的预约时间')

    def clean_slot_selection(self):
        try:
            slot_id, stamp = self.cleaned_data['slot_selection'].split('|', 1)
            if int(slot_id) <= 0 or not stamp:
                raise ValueError
            return int(slot_id), stamp
        except ValueError:
            raise forms.ValidationError('请选择有效的预约时间。') from None


def _own_booking(request, pk):
    member = portal_member_for(request.user)
    return get_object_or_404(Booking.objects.select_related('coach', 'course_type', 'previous_booking'), pk=pk, member=member)


@never_cache
@login_required(login_url='studio:portal_login')
@require_safe
def courses(request):
    portal_member_for(request.user)
    from .booking_v2 import enabled
    if enabled():
        return redirect('studio:portal_reserve')
    slots = AvailableSlot.objects.filter(active=True, starts_at__gt=timezone.now(),
        course_type__active=True, course_type__credits_cost=1, coach__status='active').select_related('course_type', 'coach')
    items = [{'slot': slot, 'available': slot_open(slot)} for slot in slots]
    return render(request, 'portal/courses.html', {'items': items})


@never_cache
@login_required(login_url='studio:portal_login')
@require_safe
def booking_confirmation(request, pk):
    portal_member_for(request.user)
    slot = get_object_or_404(AvailableSlot.objects.select_related('coach', 'course_type'), pk=pk)
    if not slot_open(slot):
        messages.error(request, '该时段已满或不可预约，请选择其他时间。')
        return redirect('studio:portal_courses')
    request_id = uuid.uuid4()
    stamp = slot_stamp(slot)
    confirmation_token = signing.dumps([request.user.pk, slot.pk, str(request_id), stamp], salt='studio.booking-confirmation')
    return render(request, 'portal/booking_confirmation.html', {
        'slot': slot, 'ends_at': slot.starts_at + timedelta(minutes=slot.duration_minutes),
        'request_id': request_id, 'stamp': stamp, 'confirmation_token': confirmation_token,
        'account': account_for(portal_member_for(request.user).pk, slot.course_type_id),
    })


@never_cache
@login_required(login_url='studio:portal_login')
@require_POST
def book_slot(request, pk):
    portal_member_for(request.user)
    form = BookForm(request.POST)
    if form.is_valid():
        try:
            data = dict(form.cleaned_data)
            token = data.pop('confirmation_token')
            try:
                displayed = signing.loads(token, salt='studio.booking-confirmation', max_age=3600)
            except signing.BadSignature:
                raise BusinessError('预约确认已失效，请重新选择时段。') from None
            if displayed != [request.user.pk, pk, str(data['request_id']), data['expected_slot']]:
                raise BusinessError('预约确认不匹配，请重新选择时段。')
            booking = member_book(actor=request.user, slot_id=pk, **data)
            if enhanced(request):
                return success(request, booking, 'book')
            messages.success(request, '预约成功，已占用1课时。')
            return redirect('studio:portal_booking_detail', pk=booking.pk)
        except BusinessError as error:
            if enhanced(request):
                return failure('；'.join(error.messages))
            messages.error(request, '；'.join(error.messages))
    else:
        if enhanced(request):
            return failure('请求无效，请重新打开课程列表。', form)
        messages.error(request, '请求无效，请重新打开课程列表。')
    return redirect('studio:portal_courses')


@never_cache
@login_required(login_url='studio:portal_login')
@require_safe
def bookings(request):
    member = portal_member_for(request.user)
    entries = Booking.objects.filter(member=member).select_related('coach').order_by('-starts_at', '-pk')
    from django.core.paginator import Paginator
    page = Paginator(entries, 20).get_page(request.GET.get('page'))
    for booking in page:
        booking.portal_locked = member_locked(booking)
    return render(request, 'portal/bookings.html', {'bookings': page})


@never_cache
@login_required(login_url='studio:portal_login')
@require_safe
def booking_detail(request, pk):
    booking = _own_booking(request, pk)
    return render(request, 'portal/booking_detail.html', {**card_context(booking), 'account': account_for(booking.member_id, booking.course_type_id)})


@never_cache
@login_required(login_url='studio:portal_login')
@require_http_methods(['GET', 'POST'])
def cancel(request, pk):
    booking = _own_booking(request, pk)
    if member_locked(booking):
        if enhanced(request) and request.method == 'POST':
            return failure('该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
        messages.error(request, '该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
        return redirect('studio:portal_booking_detail', pk=booking.pk)
    outcome = cancel_outcome(booking)
    initial = {'request_id': uuid.uuid4(), 'rule_version': RULES['cancel']['version'],
        'expected_starts_at': booking.starts_at.isoformat(), 'expected_outcome': outcome}
    form = CancelForm(request.POST or None, initial=initial)
    error = None
    if request.method == 'POST' and form.is_valid():
        try:
            result = member_cancel(booking.pk, actor=request.user, **form.cleaned_data)
            if enhanced(request):
                return success(request, result, 'cancel')
            messages.success(request, '预约已取消。')
            return redirect('studio:portal_booking_detail', pk=booking.pk)
        except BusinessError as exc:
            error = '；'.join(exc.messages)
            # A changed outcome requires a new server-rendered confirmation.
            booking.refresh_from_db()
            outcome = cancel_outcome(booking)
            form = CancelForm(initial={'request_id': uuid.uuid4(), 'rule_version': RULES['cancel']['version'],
                'expected_starts_at': booking.starts_at.isoformat(), 'expected_outcome': outcome})
    if request.method == 'POST' and enhanced(request):
        values = {key: str(form[key].value() or '') for key in ('request_id', 'rule_version', 'expected_starts_at', 'expected_outcome')}
        return failure(error or '请完整填写并勾选确认规则。', form, confirmation=values, outcome_text=outcome_text(outcome), rule_text=RULES['cancel']['text'], booking_summary=f'{booking.course_name} · {booking.coach.name} · {timezone.localtime(booking.starts_at):%Y年%m月%d日 %H:%M}')
    return render(request, 'portal/confirm.html', {'booking': booking, 'form': form, 'error': error,
        'account': account_for(booking.member_id, booking.course_type_id), 'rule': RULES['cancel'], 'action': 'cancel', 'outcome': outcome})


@never_cache
@login_required(login_url='studio:portal_login')
@require_http_methods(['GET', 'POST'])
def reschedule(request, pk):
    booking = _own_booking(request, pk)
    from .booking_v2 import enabled
    if enabled():
        return redirect('studio:portal_move', pk=pk)
    if member_locked(booking):
        if enhanced(request) and request.method == 'POST':
            return failure('该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
        messages.error(request, '该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
        return redirect('studio:portal_booking_detail', pk=booking.pk)
    slots = AvailableSlot.objects.filter(active=True, starts_at__gt=timezone.now(), course_type=booking.course_type,
        coach=booking.coach, course_type__active=True, course_type__credits_cost=1, coach__status='active').select_related('coach', 'course_type')
    options = [{'slot': slot, 'value': f'{slot.pk}|{slot_stamp(slot)}'} for slot in slots if slot.starts_at != booking.starts_at and slot_open(slot, exclude_booking=booking.pk)]
    initial = {'request_id': uuid.uuid4(), 'rule_version': RULES['reschedule']['version'],
        'expected_starts_at': booking.starts_at.isoformat()}
    form = RescheduleForm(request.POST or None, initial=initial)
    error = None
    if request.method == 'POST' and form.is_valid():
        try:
            data = dict(form.cleaned_data)
            slot_id, expected_slot = data.pop('slot_selection')
            new = member_reschedule(booking.pk, actor=request.user, slot_id=slot_id, expected_slot=expected_slot, **data)
            if enhanced(request):
                return success(request, new, 'reschedule')
            messages.success(request, '改期成功。')
            return redirect('studio:portal_booking_detail', pk=new.pk)
        except BusinessError as exc:
            error = '；'.join(exc.messages)
    if request.method == 'POST' and enhanced(request):
        return failure(error or '请完整选择时间并勾选确认规则。', form)
    return render(request, 'portal/confirm.html', {'booking': booking, 'form': form, 'error': error,
        'account': account_for(booking.member_id, booking.course_type_id), 'rule': RULES['reschedule'], 'action': 'reschedule', 'options': options})
