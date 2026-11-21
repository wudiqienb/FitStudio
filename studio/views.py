from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE, DELETION, LogEntry
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin, UserPassesTestMixin
from django.core.exceptions import BadRequest, PermissionDenied
from django.db import transaction, IntegrityError
from django.db.models.deletion import ProtectedError
from django.db.models import Q
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from django.shortcuts import get_object_or_404
from django.views.generic import FormView, CreateView, DeleteView, ListView, TemplateView, UpdateView

from .forms import MemberDeleteForm, MemberForm, MemberCreateForm, MemberAccountOpeningForm
from .models import Booking, Member, MemberAccount


class StaffPageMixin(LoginRequiredMixin, UserPassesTestMixin):
    """当前是工作室操作员界面；会员本人自助登录仍不在本阶段范围。"""

    def test_func(self):
        return self.request.user.is_active and self.request.user.is_staff


class HomeView(StaffPageMixin, TemplateView):
    template_name = "studio/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.user.has_perm("studio.view_booking"):
            context["today_bookings"] = Booking.objects.filter(starts_at__date=timezone.localdate(), status__in=[Booking.Status.PENDING, Booking.Status.COMPLETED]).select_related("member", "coach")[:6]
            context["pending_bookings"] = Booking.objects.filter(status="pending").select_related("member", "coach")[:4]
            context["makeup_due"] = Booking.objects.filter(Q(status=Booking.Status.COACH_CANCELLED) | Q(status=Booking.Status.CANCELLED, makeup_origin__isnull=False), replacement__isnull=True).select_related("member", "coach")[:6]
        return context


class CoursesView(StaffPageMixin, TemplateView):
    template_name = "studio/courses.html"


class MoreView(StaffPageMixin, TemplateView):
    template_name = "studio/more.html"


class MemberListView(StaffPageMixin, PermissionRequiredMixin, ListView):
    model = Member
    permission_required = "studio.view_member"
    template_name = "studio/member_list.html"
    paginate_by = 12

    def get_queryset(self):
        queryset = super().get_queryset().select_related("login_account__user").exclude(status=Member.Status.ARCHIVED)
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if status and status not in Member.Status.values:
            raise BadRequest("无效的会员状态筛选")
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(phone__icontains=query))
        if status:
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .archive_services import can_archive_member
        context['can_archive_member'] = can_archive_member(self.request.user)
        context['can_reset_credentials'] = self.request.user.has_perm('studio.reset_member_credentials')
        context.update(q=self.request.GET.get("q", "").strip(),
                       selected_status=self.request.GET.get("status", ""),
                       statuses=[(value,label) for value,label in Member.Status.choices if value != Member.Status.ARCHIVED])
        if self.request.user.has_perm("studio.view_lessonentry"):
            from .models import LessonEntry
            from .services import credit_snapshot
            for member in context["object_list"]:
                courses = {entry.course_type for entry in LessonEntry.objects.filter(member=member).select_related("course_type")}
                member.credit_summaries = [dict(course=course, **credit_snapshot(member.pk, course.pk)) for course in courses]
        return context


class MemberWriteMixin(StaffPageMixin, PermissionRequiredMixin):
    model = Member
    form_class = MemberForm
    template_name = "studio/member_form.html"
    success_url = reverse_lazy("studio:members")

    def after_member_save(self, form):
        pass

    def form_valid(self, form):
        is_new = self.object is None
        with transaction.atomic():
            if not is_new:
                current = Member.objects.select_for_update().get(pk=self.object.pk)
                if current.status == Member.Status.ARCHIVED:
                    raise PermissionDenied('该会员已归档，请使用正式恢复流程。')
            response = super().form_valid(form)
            self.after_member_save(form)
            LogEntry.objects.log_actions(
                self.request.user.pk, [self.object], ADDITION if is_new else CHANGE,
                change_message="手机业务页：新增会员" if is_new else
                "手机业务页：修改 " + "、".join(str(form.fields[name].label) for name in form.changed_data),
            )
        messages.success(self.request, "会员档案已保存。")
        return response


def open_login_account(form, member, actor):
    if not (actor.is_active and actor.is_staff and actor.is_superuser):
        raise PermissionDenied
    if member.status == Member.Status.ARCHIVED:
        raise PermissionDenied('请先恢复归档会员，再管理登录账号。')
    user = form.save()
    binding = MemberAccount.objects.create(user=user, member=member)
    LogEntry.objects.log_actions(actor.pk, [binding], ADDITION,
                                 change_message='教练端：开通普通会员登录账号')
    return binding


@method_decorator(sensitive_post_parameters('name', 'phone', 'notes', 'username', 'password1', 'password2'), name='dispatch')
class MemberCreateView(MemberWriteMixin, CreateView):
    permission_required = "studio.add_member"
    form_class = MemberCreateForm

    def get_form_kwargs(self):
        if self.request.method == 'POST' and self.request.POST.get('open_account') and not self.request.user.is_superuser:
            raise PermissionDenied
        if self.request.method == 'POST' and self.request.POST.get('open_account') and not self.request.user.has_perm('studio.add_memberaccount'):
            raise PermissionDenied
        return dict(super().get_form_kwargs(), allow_account=self.request.user.is_superuser)

    def after_member_save(self, form):
        if form.cleaned_data.get('open_account'):
            open_login_account(form.account_form, self.object, self.request.user)

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except IntegrityError:
            self.object = None
            form.instance.pk = None
            form.add_error(None, '账号名已被使用或开户状态已变化，请核对后重试。')
            return self.form_invalid(form)


@method_decorator(sensitive_post_parameters('username', 'password1', 'password2'), name='dispatch')
class MemberAccountCreateView(StaffPageMixin, PermissionRequiredMixin, FormView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    permission_required = ('studio.view_member', 'studio.add_memberaccount')
    form_class = MemberAccountOpeningForm
    template_name = 'studio/member_account_form.html'
    success_url = reverse_lazy('studio:members')

    def get_context_data(self, **kwargs):
        return dict(super().get_context_data(**kwargs), member=get_object_or_404(Member, pk=self.kwargs['pk']))

    def form_valid(self, form):
        try:
            with transaction.atomic():
                member = get_object_or_404(Member.objects.select_for_update(), pk=self.kwargs['pk'])
                if MemberAccount.objects.filter(member=member).exists():
                    form.add_error(None, '该会员已开通账号，不能重复开通或替换绑定。')
                    return self.form_invalid(form)
                open_login_account(form, member, self.request.user)
        except IntegrityError:
            form.add_error(None, '账号名已被使用或该会员已开通账号，请刷新核对后重试。')
            return self.form_invalid(form)
        messages.success(self.request, '会员登录账号已开通，可使用设置的账号和密码登录会员端。')
        return super().form_valid(form)


@method_decorator(sensitive_post_parameters('name', 'phone', 'notes'), name='dispatch')
class MemberUpdateView(MemberWriteMixin, UpdateView):
    def get_queryset(self):
        return super().get_queryset().exclude(status=Member.Status.ARCHIVED)

    permission_required = "studio.change_member"


class MemberDeleteView(StaffPageMixin, PermissionRequiredMixin, DeleteView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            raise PermissionDenied('会员档案仅支持管理员归档，不支持永久删除。')
        return super().dispatch(request, *args, **kwargs)

    model = Member
    permission_required = "studio.delete_member"
    form_class = MemberDeleteForm
    template_name = "studio/member_confirm_delete.html"
    success_url = reverse_lazy("studio:members")

    def form_valid(self, form):
        try:
            with transaction.atomic():
                LogEntry.objects.log_actions(self.request.user.pk, [self.object], DELETION,
                                             change_message="手机业务页：确认删除会员")
                response = super().form_valid(form)
        except ProtectedError:
            form.add_error(None, "该会员有关联购课或预约历史，不能删除，请使用停用。")
            return self.form_invalid(form)
        messages.success(self.request, "会员档案已删除。")
        return response
