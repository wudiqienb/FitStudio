import uuid
from datetime import datetime
from django import forms
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.contrib import messages
from django.shortcuts import render,redirect,get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods,require_POST
from django.utils import timezone
from .models import Booking
from .portal_access import portal_member_for
from .portal_rules import RULES
from .portal_services import member_locked
from .services import BusinessError,credit_snapshot
from .booking_v2 import configuration,validate_time,free_times,book_direct,reschedule_direct

class TimeForm(forms.Form):
    day=forms.DateField(label='预约日期',widget=forms.DateInput(attrs={'type':'date','class':'portal-input'}))
    start_time=forms.TimeField(label='开始时间',widget=forms.TimeInput(attrs={'type':'time','step':'1800','class':'portal-input'}))
    def starts_at(self):return timezone.make_aware(datetime.combine(self.cleaned_data['day'],self.cleaned_data['start_time']))

@never_cache
@login_required(login_url='studio:portal_login')
@require_http_methods(['GET','POST'])
def reserve(request,pk=None):
    member=portal_member_for(request.user)
    booking=get_object_or_404(Booking,pk=pk,member=member) if pk else None
    error=None;options=[];account=None;days=None
    day=timezone.localdate()
    try:day=forms.DateField().clean(request.POST.get('day') or request.GET.get('day') or day)
    except forms.ValidationError:error='请选择有效日期。'
    form=TimeForm(request.POST or None,initial={'day':day})
    try:
        coach,course,days=configuration();account=credit_snapshot(member.pk,course.pk)
        if booking and member_locked(booking):raise BusinessError('该课程已经改期，无法再次改期或取消。如需调整，请联系教练。')
        options=free_times(member,day,booking=booking)
        if request.method=='POST' and form.is_valid():
            start=form.starts_at();duration=int((booking.ends_at-booking.starts_at).total_seconds()/60) if booking else 60
            end=validate_time(start,duration)
            if timezone.localtime(start).strftime('%H:%M') not in [x[0] for x in options]:raise BusinessError('该时间已被占用或不可预约，请重新选择。')
            payload=dict(user=request.user.pk,start=start.isoformat(),coach=coach.pk,course=course.pk,duration=duration,key=str(uuid.uuid4()),booking=booking.pk if booking else None,old=booking.starts_at.isoformat() if booking else None)
            token=signing.dumps(payload,salt='studio.direct-confirm-v2')
            return render(request,'portal/direct_confirmation.html',dict(confirmation_token=token,starts_at=start,ends_at=end,booking=booking,account=account,coach=coach,rule=RULES['reschedule'] if booking else None))
    except BusinessError as exc:error='；'.join(exc.messages)
    return render(request,'portal/direct_reserve.html',dict(form=form,error=error,options=options,day=day,booking=booking,account=account,days=days))

@never_cache
@login_required(login_url='studio:portal_login')
@require_POST
def submit(request):
    portal_member_for(request.user)
    data=None
    try:
        try:data=signing.loads(request.POST.get('confirmation_token',''),salt='studio.direct-confirm-v2',max_age=3600)
        except signing.BadSignature:raise BusinessError('预约确认已失效，请重新选择。') from None
        if data['user']!=request.user.pk:raise BusinessError('预约确认不匹配，请重新选择。')
        start=datetime.fromisoformat(data['start'])
        # Refuse stale coach/course configuration. Never accept identifiers from the browser.
        coach,course,_=configuration()
        if (coach.pk,course.pk)!=(data['coach'],data['course']):raise BusinessError('预约配置已变化，请重新选择。')
        if data['booking']:
            result=reschedule_direct(data['booking'],actor=request.user,starts_at=start,request_id=data['key'],confirmed=request.POST.get('confirmed')=='on',rule_version=request.POST.get('rule_version'),expected_starts_at=data['old'])
        else:result=book_direct(actor=request.user,starts_at=start,request_id=data['key'])
        messages.success(request,'改期成功，会员端修改已锁定。' if data['booking'] else '预约成功，已占用1节课时，尚未扣课。')
        return redirect('studio:portal_booking_detail',pk=result.pk)
    except BusinessError as exc:
        messages.error(request,'；'.join(exc.messages))
        if data and data.get('user')==request.user.pk and data.get('booking'):
            return redirect('studio:portal_move',pk=data['booking'])
        return redirect('studio:portal_reserve')
