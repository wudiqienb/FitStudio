from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .archive_services import (archive_member, archive_summary, require_archive_permission,
                               restore_member)
from .models import Member
from .services import BusinessError


class ArchiveConfirmationForm(forms.Form):
    reason = forms.CharField(label='操作原因（选填）', required=False, max_length=200,
        widget=forms.Textarea(attrs={'class':'form-control', 'rows':2}))
    confirm = forms.BooleanField(label='我已核对会员姓名、电话及课时，确认本次操作')
    acknowledge_bookings = forms.BooleanField(required=False,
        label='我已核对待处理或未来预约，知晓归档不会自动取消预约')

    def __init__(self, *args, has_bookings=False, restore=False, **kwargs):
        super().__init__(*args, **kwargs)
        if restore or not has_bookings:
            self.fields.pop('acknowledge_bookings')
        else:
            self.fields['acknowledge_bookings'].required = True


@login_required
@require_http_methods(['GET', 'POST'])
def member_archive(request, pk):
    return _confirmation(request, pk, restore=False)


@login_required
@require_http_methods(['GET', 'POST'])
def member_restore(request, pk):
    return _confirmation(request, pk, restore=True)


def _confirmation(request, pk, restore):
    require_archive_permission(request.user)
    member = get_object_or_404(Member, pk=pk)
    summary = archive_summary(member)
    form = ArchiveConfirmationForm(request.POST if request.method == 'POST' else None,
        has_bookings=summary['attention_bookings'].exists(), restore=restore)
    if request.method == 'POST' and form.is_valid():
        try:
            kwargs = dict(member_id=member.pk, actor=request.user, reason=form.cleaned_data['reason'])
            if restore:
                restore_member(**kwargs)
            else:
                archive_member(**kwargs, acknowledge_bookings=form.cleaned_data.get('acknowledge_bookings', False))
        except BusinessError as error:
            form.add_error(None, error)
        else:
            messages.success(request, '会员已恢复，原有课时及历史已保留。' if restore else '会员已归档，账号已停用，历史及课时已保留。')
            return redirect('studio:members' if restore else 'studio:archived_member_detail', **({} if restore else {'pk':member.pk}))
    return render(request, 'studio/archive_confirm.html', dict(member=member, form=form,
        restore=restore, **summary))


@login_required
@require_http_methods(['GET'])
def archived_members(request):
    require_archive_permission(request.user)
    return render(request, 'studio/archive_list.html', {'members':Member.objects.filter(
        status=Member.Status.ARCHIVED)})


@login_required
@require_http_methods(['GET'])
def archived_member_detail(request, pk):
    require_archive_permission(request.user)
    member = get_object_or_404(Member, pk=pk, status=Member.Status.ARCHIVED)
    return render(request, 'studio/archive_detail.html', dict(member=member,
        events=member.archive_events.select_related('created_by'),
        purchases=member.purchase_set.select_related('course_type'),
        bookings=member.booking_set.select_related('coach', 'course_type'),
        entries=member.lessonentry_set.select_related('course_type'), **archive_summary(member)))
