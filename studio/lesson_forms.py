import uuid
from datetime import datetime

from django import forms
from django.utils import timezone

from .models import Coach, CourseType, Member, Package


class TouchFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.HiddenInput):
                continue
            field.widget.attrs["class"] = ("form-check-input" if isinstance(field.widget, forms.CheckboxInput)
                else "form-select" if isinstance(field.widget, forms.Select) else "form-control")


class CourseTypeForm(TouchFormMixin, forms.ModelForm):
    class Meta:
        model = CourseType
        fields = ("name", "duration_minutes", "credits_cost", "active")


class PackageForm(TouchFormMixin, forms.ModelForm):
    class Meta:
        model = Package
        fields = ("name", "course_type", "credits", "price", "active")


class PurchaseForm(TouchFormMixin, forms.Form):
    member = forms.ModelChoiceField(label="会员", queryset=Member.objects.filter(status="active"))
    package = forms.ModelChoiceField(label="课时套餐", queryset=Package.objects.filter(active=True, course_type__active=True))
    purchased_on = forms.DateField(label="购买日期", initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    purchased_time = forms.TimeField(label="实际购买时间", required=False, help_text="时间不确定时留空，不会虚构时间。", widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}))
    credits = forms.IntegerField(label="实际购买课时", required=False, min_value=1, help_text="留空使用套餐课时。")
    amount = forms.DecimalField(label="登记金额（元）", required=False, min_value=0, max_digits=10,
        decimal_places=2, help_text="留空使用套餐金额；此处仅登记，不发起支付。")
    reason = forms.CharField(label="登记原因", max_length=200, initial="购课登记")
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def occurred_at(self):
        time = self.cleaned_data.get("purchased_time")
        return timezone.make_aware(datetime.combine(self.cleaned_data["purchased_on"], time), timezone.get_default_timezone()) if time else None


class BookingTimeForm(TouchFormMixin, forms.Form):
    day = forms.DateField(label="上课日期", initial=timezone.localdate,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}))
    start_time = forms.TimeField(label="开始时间", widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}))
    duration_minutes = forms.IntegerField(label="时长（分钟）", min_value=15, max_value=240, required=False,
        help_text="留空采用所选课程默认时长；手动填写须为15～240分钟，每次消耗课时由课程类型决定。")

    def starts_at(self):
        return timezone.make_aware(datetime.combine(self.cleaned_data["day"], self.cleaned_data["start_time"]), timezone.get_default_timezone())


class BookingForm(BookingTimeForm):
    member = forms.ModelChoiceField(label="会员", queryset=Member.objects.filter(status="active"))
    coach = forms.ModelChoiceField(label="教练", queryset=Coach.objects.filter(status="active"))
    course_type = forms.ModelChoiceField(label="课程类型", queryset=CourseType.objects.filter(active=True))
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    field_order = ["member", "coach", "course_type", "day", "start_time", "duration_minutes"]


class RescheduleForm(BookingTimeForm):
    duration_minutes = forms.IntegerField(label="时长（分钟）", min_value=15, max_value=240)
    reason = forms.CharField(label="改期原因", max_length=200)
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4, required=False)


class BookingActionForm(TouchFormMixin, forms.Form):
    reason = forms.CharField(label="操作原因", max_length=200)


class CancelForm(BookingActionForm):
    expected_outcome = forms.ChoiceField(choices=[("normal", "正常取消"), ("late", "迟取消")], widget=forms.HiddenInput)
    expected_starts_at = forms.CharField(widget=forms.HiddenInput)


class MakeupForm(BookingTimeForm):
    coach = forms.ModelChoiceField(label="教练", queryset=Coach.objects.filter(status="active"))
    reason = forms.CharField(label="安排原因", max_length=200)
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)


class BackfillForm(BookingForm):
    reason = forms.CharField(label="补录原因", max_length=200)
    field_order = ["member", "coach", "course_type", "day", "start_time", "duration_minutes", "reason"]


class AdjustmentForm(TouchFormMixin, forms.Form):
    member = forms.ModelChoiceField(label="会员", queryset=Member.objects.all())
    course_type = forms.ModelChoiceField(label="课程类型", queryset=CourseType.objects.all())
    delta = forms.IntegerField(label="调整课时", help_text="增加填正数，减少填负数。")
    day = forms.DateField(label="实际发生日期", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(label="实际发生时间", widget=forms.TimeInput(attrs={"type": "time"}))
    reason = forms.CharField(label="调整原因", max_length=200)
    request_id = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)

    def occurred_at(self):
        return timezone.make_aware(datetime.combine(self.cleaned_data["day"], self.cleaned_data["start_time"]), timezone.get_default_timezone())
