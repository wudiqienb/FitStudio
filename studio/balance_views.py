from django.contrib.auth.decorators import login_required,permission_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import render,get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from .models import Member,CourseType
from .services import credit_snapshot

@login_required
@permission_required('studio.view_member',raise_exception=True)
@never_cache
@require_safe
def member_balance(request,pk):
    if not request.user.is_active or not request.user.is_staff:raise PermissionDenied
    member=get_object_or_404(Member,pk=pk)
    if member.status=='archived':
        from .archive_services import can_archive_member
        if not can_archive_member(request.user):raise PermissionDenied
    accounts=[dict(course=course,**credit_snapshot(member.pk,course.pk)) for course in CourseType.objects.all()]
    return render(request,'studio/member_balance.html',dict(member=member,accounts=accounts))
