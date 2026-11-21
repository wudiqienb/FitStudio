from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import Http404, JsonResponse
from django.template.loader import render_to_string
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import CreateView, DetailView, FormView, TemplateView

from .forms import MemberForm
from .lesson_forms import (BookingActionForm, BookingForm, CourseTypeForm, PackageForm, PurchaseForm, RescheduleForm, CancelForm, MakeupForm, BackfillForm, AdjustmentForm)
from .models import Booking, CourseType, LessonEntry, Member, Package
from .services import (BusinessError, cancel_booking, complete_booking, credit_snapshot,
    create_booking, register_purchase, reschedule_booking, restore_booking, cancel_outcome,
    mark_no_show, coach_cancel_booking, create_makeup_booking, record_historical_lesson, adjust_credits)
from .views import StaffPageMixin


class CatalogView(StaffPageMixin, PermissionRequiredMixin, TemplateView):
    permission_required = "studio.view_coursetype"
    template_name = "studio/catalog.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        can_view_packages = self.request.user.has_perm("studio.view_package")
        context.update(course_types=CourseType.objects.all(), can_view_packages=can_view_packages,
            packages=Package.objects.select_related("course_type").all() if can_view_packages else Package.objects.none())
        return context


class CourseCreateView(StaffPageMixin, PermissionRequiredMixin, CreateView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    permission_required = "studio.add_coursetype"
    form_class = CourseTypeForm
    template_name = "studio/lesson_form.html"
    success_url = reverse_lazy("studio:catalog")
    extra_context = {"page_title": "新增课程类型", "intro": "设置上课时长和每次消耗课时。", "submit_label": "保存课程"}


class PackageCreateView(StaffPageMixin, PermissionRequiredMixin, CreateView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.is_superuser:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    permission_required = "studio.add_package"
    form_class = PackageForm
    template_name = "studio/lesson_form.html"
    success_url = reverse_lazy("studio:catalog")
    extra_context = {"page_title": "新增课时套餐", "intro": "同一课程类型下的课时可以合并使用。", "submit_label": "保存套餐"}


class BusinessFormView(StaffPageMixin, PermissionRequiredMixin, FormView):
    template_name = "studio/lesson_form.html"

    def interaction_enabled(self):
        return isinstance(self, (BookingMakeupView, LessonAdjustmentView)) or (isinstance(self, BookingActionView) and self.kwargs['action'] in ('complete', 'no-show', 'coach-cancel', 'restore'))

    def wants_interaction(self):
        return self.interaction_enabled() and self.request.headers.get('X-FitStudio-Interaction') == '1'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['coach_interaction'] = self.interaction_enabled()
        context['cautious_interaction'] = self.interaction_enabled() and not (isinstance(self, BookingActionView) and self.kwargs['action'] == 'complete')
        return context

    def form_invalid(self, form):
        if self.wants_interaction():
            errors = form.errors.get_json_data()
            text = '；'.join(item['message'] for items in errors.values() for item in items)
            return JsonResponse({'ok': False, 'message': text, 'errors': errors}, status=422)
        return super().form_invalid(form)

    def interaction_result(self, destination):
        booking = getattr(self, 'result_booking', None)
        adjustment = getattr(self, 'result_adjustment', None)
        if booking:
            booking.refresh_from_db()
            account = credit_snapshot(booking.member_id, booking.course_type_id)
            action = self.kwargs.get('action', 'makeup')
            kind = {'complete': 'complete', 'no-show': 'no_show', 'restore': 'restore'}.get(action)
            entry = LessonEntry.objects.filter(booking=booking, kind=kind).first() if kind else None
            if action in ('complete', 'no-show'):
                message = ('课程已完成' if action == 'complete' else '爽约已确认') + f'，已扣除{abs(entry.delta)}节课时。'
            elif action == 'restore':
                message = f'冲正成功，已恢复{entry.delta}节课时。'
            elif action == 'coach-cancel':
                message = '教练取消成功，不扣课；' + ('补课已安排。' if getattr(booking, 'replacement', None) else '待安排补课。')
            else:
                message = '补课已安排，预约占用可用课时，完成后才扣课。'
        else:
            account = credit_snapshot(adjustment.member_id, adjustment.course_type_id)
            entry = LessonEntry.objects.get(adjustment=adjustment)
            message = f'课时调整成功，本次调整{entry.delta:+d}节课时。'
        ledger = {'before': entry.balance_before, 'after': entry.balance_after, 'delta': entry.delta} if entry else None
        html = render_to_string('studio/interaction_result.html', {'booking': booking, 'account': account, 'ledger': ledger, 'destination': destination}, request=self.request)
        return {'ok': True, 'message': message, 'html': html, 'account': account, 'ledger': ledger}

    def form_valid(self, form):
        try:
            destination = self.execute(form)
        except BusinessError as error:
            form.add_error(None, error)
            return self.form_invalid(form)
        if self.interaction_enabled():
            result = self.interaction_result(destination)
            if self.wants_interaction():
                return JsonResponse(result)
            messages.success(self.request, result['message'])
        else:
            messages.success(self.request, "操作已登记。重复提交不会重复入账或扣课。")
        return redirect(destination)


class PurchaseCreateView(BusinessFormView):
    permission_required = "studio.add_purchase"
    form_class = PurchaseForm
    extra_context = {"page_title": "登记会员购课", "intro": "购课成功后增加课时，并保留入账流水。", "submit_label": "确认购课入账"}

    def get_initial(self):
        initial = super().get_initial()
        if self.request.GET.get("member", "").isdigit():
            initial["member"] = self.request.GET["member"]
        return initial

    def execute(self, form):
        data = form.cleaned_data
        purchase = register_purchase(member_id=data["member"].pk, package_id=data["package"].pk,
            purchased_on=data["purchased_on"], credits=data["credits"], amount=data["amount"],
            reason=data["reason"], request_id=data["request_id"], actor=self.request.user, occurred_at=form.occurred_at())
        return reverse_lazy("studio:member_lessons", args=[purchase.member_id])


class MemberLessonsView(StaffPageMixin, PermissionRequiredMixin, DetailView):
    permission_required = "studio.view_lessonentry"
    model = Member
    template_name = "studio/member_lessons.html"

    def get_context_data(self, **kwargs):
        from .archive_services import can_archive_member
        if self.object.status == Member.Status.ARCHIVED and not can_archive_member(self.request.user):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        context = super().get_context_data(**kwargs)
        context["accounts"] = [dict(course=course, **credit_snapshot(self.object.pk, course.pk)) for course in CourseType.objects.all()]
        kind = self.request.GET.get("kind", "")
        entries = LessonEntry.objects.filter(member=self.object).select_related("course_type", "created_by", "purchase")
        if kind in LessonEntry.Kind.values:
            entries = entries.filter(kind=kind)
        context.update(entries=Paginator(entries, 20).get_page(self.request.GET.get("page")), kinds=LessonEntry.Kind.choices, selected_kind=kind)
        return context


class CourseListView(StaffPageMixin, PermissionRequiredMixin, TemplateView):
    permission_required = "studio.view_booking"
    template_name = "studio/courses.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            day = date.fromisoformat(self.request.GET.get("date") or timezone.localdate().isoformat())
        except ValueError:
            day = timezone.localdate()
            context["date_error"] = "日期格式无效，已显示今天的课程。"
        history = self.request.GET.get("history") == "1"
        bookings = Booking.objects.filter(starts_at__date=day).select_related("member", "coach", "course_type")
        if not history:
            bookings = bookings.filter(status__in=[Booking.Status.PENDING, Booking.Status.COMPLETED])
        context.update(selected_day=day, bookings=bookings, history=history)
        return context


class BookingCreateView(BusinessFormView):
    permission_required = "studio.add_booking"
    form_class = BookingForm
    extra_context = {"page_title": "创建课程预约", "intro": "预约占用可用额度，完成课程后才扣课。", "submit_label": "确认预约"}

    def execute(self, form):
        data = form.cleaned_data
        booking = create_booking(member_id=data["member"].pk, coach_id=data["coach"].pk,
            course_type_id=data["course_type"].pk, starts_at=form.starts_at(), duration_minutes=data["duration_minutes"],
            request_id=data["request_id"], actor=self.request.user)
        self.result_booking = booking
        return reverse_lazy("studio:booking_detail", args=[booking.pk])


class BookingDetailView(StaffPageMixin, PermissionRequiredMixin, DetailView):
    permission_required = "studio.view_booking"
    model = Booking
    template_name = "studio/booking_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["events"] = self.object.events.select_related("created_by").all()
        return context


class BookingRescheduleView(BusinessFormView):
    permission_required = "studio.change_booking"
    form_class = RescheduleForm
    extra_context = {"page_title": "预约改期", "intro": "保留会员、教练和扣课快照，重新检查时间冲突。", "submit_label": "确认改期"}

    def get_initial(self):
        booking = get_object_or_404(Booking, pk=self.kwargs["pk"])
        local = timezone.localtime(booking.starts_at)
        return dict(day=local.date(), start_time=local.time(), duration_minutes=int((booking.ends_at-booking.starts_at).total_seconds()/60))

    def execute(self, form):
        booking = reschedule_booking(self.kwargs["pk"], starts_at=form.starts_at(), duration_minutes=form.cleaned_data["duration_minutes"],
            actor=self.request.user, reason=form.cleaned_data["reason"], request_id=form.cleaned_data["request_id"])
        self.result_booking = booking
        return reverse_lazy("studio:booking_detail", args=[booking.pk])


class BookingActionView(BusinessFormView):
    form_class = BookingActionForm
    actions = {"complete": ("complete_booking", "确认完成并扣课", complete_booking),
               "cancel": ("change_booking", "取消预约", cancel_booking),
               "no-show": ("no_show_booking", "登记会员爽约", mark_no_show),
               "coach-cancel": ("coach_cancel_booking", "登记教练取消", coach_cancel_booking),
               "restore": ("restore_booking", "冲正并恢复误扣课时", restore_booking)}

    def get_permission_required(self):
        if self.kwargs["action"] not in self.actions:
            raise Http404
        return ("studio." + self.actions[self.kwargs["action"]][0],)

    def get_initial(self):
        booking = get_object_or_404(Booking, pk=self.kwargs["pk"])
        return {"reason": "确认课程完成" if self.kwargs["action"] == "complete" else "",
                "expected_outcome": cancel_outcome(booking), "expected_starts_at": booking.starts_at.isoformat()}

    def get_form_class(self):
        return CancelForm if self.kwargs["action"] == "cancel" else BookingActionForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        booking = get_object_or_404(Booking, pk=self.kwargs["pk"])
        action = self.kwargs["action"]
        outcome = cancel_outcome(booking)
        descriptions = {"complete": "确认课程已结束，将扣除一次课时。", "no-show": "确认会员未到场，将扣除一次课时。",
                        "coach-cancel": "教练取消不扣课，之后可安排关联补课。", "restore": "冲正将通过新流水恢复课时，保留原记录。"}
        intro = descriptions.get(action, {"normal": "正常取消：不扣课，释放占用。", "late": "迟取消：将扣除一次课时。", "started": "课程已开始，请选择适当的状态操作。"}[outcome])
        can_submit = not (action == "cancel" and (outcome == "started" or (outcome == "late" and not self.request.user.has_perm("studio.late_cancel_booking"))))
        if action == "cancel" and context["form"].is_bound and (context["form"].data.get("expected_outcome") != outcome or context["form"].data.get("expected_starts_at") != booking.starts_at.isoformat()):
            can_submit = False
            intro += " 结果已变化，请重新打开确认页面。"
        context.update(page_title=self.actions[action][1], submit_label="确认操作", intro=intro, booking=booking, can_submit=can_submit)
        return context

    def execute(self, form):
        args = dict(actor=self.request.user, reason=form.cleaned_data["reason"])
        if self.kwargs["action"] == "cancel":
            args.update(expected_outcome=form.cleaned_data["expected_outcome"], expected_starts_at=form.cleaned_data["expected_starts_at"])
        booking = self.actions[self.kwargs["action"]][2](self.kwargs["pk"], **args)
        self.result_booking = booking
        return reverse_lazy("studio:booking_detail", args=[booking.pk])


class BookingMakeupView(BusinessFormView):
    permission_required = "studio.makeup_booking"
    form_class = MakeupForm
    extra_context = {"page_title": "安排补课", "intro": "补课预约占用课时，完成后才扣一次。", "submit_label": "确认安排补课"}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["booking"] = get_object_or_404(Booking, pk=self.kwargs["pk"])
        return context

    def execute(self, form):
        data = form.cleaned_data
        booking = create_makeup_booking(self.kwargs["pk"], starts_at=form.starts_at(), duration_minutes=data["duration_minutes"], coach_id=data["coach"].pk, actor=self.request.user, request_id=data["request_id"], reason=data["reason"])
        self.result_booking = booking
        return reverse_lazy("studio:booking_detail", args=[booking.pk])


class BookingBackfillView(BusinessFormView):
    permission_required = "studio.backfill_booking"
    form_class = BackfillForm
    extra_context = {"page_title": "补录历史课程", "intro": "填写实际发生的时间；课程必须已经结束，确认后扣课。", "submit_label": "确认补录并扣课"}

    def execute(self, form):
        data = form.cleaned_data
        booking = record_historical_lesson(member_id=data["member"].pk, coach_id=data["coach"].pk, course_type_id=data["course_type"].pk, starts_at=form.starts_at(), duration_minutes=data["duration_minutes"], actor=self.request.user, request_id=data["request_id"], reason=data["reason"])
        self.result_booking = booking
        return reverse_lazy("studio:booking_detail", args=[booking.pk])


class LessonAdjustmentView(BusinessFormView):
    permission_required = "studio.add_lessonadjustment"
    form_class = AdjustmentForm
    extra_context = {"page_title": "课时调整", "intro": "填写实际发生时间和原因，系统将保留调整流水。", "submit_label": "确认调整"}

    def get_initial(self):
        return {"member": self.request.GET.get("member")}

    def execute(self, form):
        data = form.cleaned_data
        adjustment = adjust_credits(member_id=data["member"].pk, course_type_id=data["course_type"].pk, delta=data["delta"], occurred_at=form.occurred_at(), actor=self.request.user, request_id=data["request_id"], reason=data["reason"])
        self.result_adjustment = adjustment
        return reverse_lazy("studio:member_lessons", args=[adjustment.member_id])
