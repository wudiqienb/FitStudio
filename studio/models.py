import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class Archive(models.Model):
    """会员和教练共用的档案字段；抽象模型本身不建立数据表。"""

    name = models.CharField("姓名", max_length=80)
    phone = models.CharField("联系电话", max_length=32, blank=True)
    notes = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        self.name = (self.name or "").strip()
        if not self.name:
            raise ValidationError({"name": "请填写姓名，不能只包含空格。"})

    def __str__(self):
        return self.name


class Member(Archive):
    class Status(models.TextChoices):
        ACTIVE = "active", "正常"
        INACTIVE = "inactive", "停用"
        ARCHIVED = "archived", "已归档"

    status = models.CharField("会员状态", max_length=16, choices=Status.choices, default=Status.ACTIVE)
    joined_on = models.DateField("入会日期", default=timezone.localdate)

    class Meta(Archive.Meta):
        verbose_name = "会员"
        verbose_name_plural = "会员档案"
        permissions = [("quick_open_member", "快速新增会员"), ("quick_renew_member", "老会员续课"),
                       ("archive_member", "归档和恢复会员")]
        constraints = [models.CheckConstraint(
            condition=models.Q(status__in=["active", "inactive", "archived"]), name="member_valid_status"
        )]


class ImmutableArchiveQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("归档操作记录不可修改。")

    def delete(self):
        raise ValidationError("归档操作记录不可删除。")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("归档操作记录不可修改。")


class MemberArchiveEvent(models.Model):
    member = models.ForeignKey(Member, on_delete=models.PROTECT, related_name='archive_events')
    action = models.CharField(max_length=8, choices=[('archive', '归档'), ('restore', '恢复')])
    reason = models.CharField(max_length=200, blank=True)
    previous_status = models.CharField(max_length=16)
    account_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name='member_archive_account_events')
    previous_user_active = models.BooleanField(null=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='member_archive_actions')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableArchiveQuerySet.as_manager()

    class Meta:
        ordering = ['-created_at', '-pk']
        default_permissions = ()
        verbose_name = '会员归档操作记录'
        constraints = [models.CheckConstraint(condition=models.Q(action__in=['archive', 'restore']),
            name='member_archive_action_valid')]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("归档操作记录不可修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("归档操作记录不可删除。")


class MemberAccount(models.Model):
    """管理员开通的登录绑定；不改变会员档案或课务历史。"""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="member_account", verbose_name="登录账号")
    member = models.OneToOneField(Member, on_delete=models.PROTECT,
        related_name="login_account", verbose_name="会员档案")
    created_at = models.DateTimeField("开通时间", auto_now_add=True)

    class Meta:
        verbose_name = "会员登录绑定"
        verbose_name_plural = "会员登录绑定"
        ordering = ["-created_at", "-pk"]

    def clean(self):
        super().clean()
        if self.user_id and (self.user.is_staff or self.user.is_superuser):
            raise ValidationError({"user": "请选择普通会员账号，不能绑定职员或管理员账号。"})
        previous = type(self).objects.select_related('member').filter(pk=self.pk).first() if self.pk else None
        changed = previous is None or previous.member_id != self.member_id or previous.user_id != self.user_id
        if changed and ((self.member_id and self.member.status == Member.Status.ARCHIVED) or
                        (previous is not None and previous.member.status == Member.Status.ARCHIVED)):
            raise ValidationError({'member': '归档期间不能新增或更换账号绑定，请先恢复会员。'})

    def __str__(self):
        return f"{self.member} · {self.user.get_username()}"


class Coach(Archive):
    class Status(models.TextChoices):
        ACTIVE = "active", "在职"
        LEFT = "left", "离职"

    specialty = models.CharField("擅长领域", max_length=200, blank=True)
    status = models.CharField("在职状态", max_length=16, choices=Status.choices, default=Status.ACTIVE)

    class Meta(Archive.Meta):
        verbose_name = "教练"
        verbose_name_plural = "教练档案"
        constraints = [models.CheckConstraint(
            condition=models.Q(status__in=["active", "left"]), name="coach_valid_status"
        )]


class CourseType(models.Model):
    name = models.CharField("课程名称", max_length=80)
    duration_minutes = models.PositiveSmallIntegerField("默认时长（分钟）", default=60,
        validators=[MinValueValidator(15), MaxValueValidator(240)])
    credits_cost = models.PositiveSmallIntegerField("每次消耗课时", default=1, validators=[MinValueValidator(1)])
    active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "课程类型"
        verbose_name_plural = "课程类型"
        ordering = ["name", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(credits_cost__gte=1), name="course_cost_positive"),
                       models.CheckConstraint(condition=models.Q(duration_minutes__gte=15, duration_minutes__lte=240), name="course_duration_valid")]

    def __str__(self):
        return self.name


class Package(models.Model):
    name = models.CharField("套餐名称", max_length=80)
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    credits = models.PositiveIntegerField("套餐课时", validators=[MinValueValidator(1)])
    price = models.DecimalField("参考金额（元）", max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "课时套餐"
        verbose_name_plural = "课时套餐"
        ordering = ["-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(credits__gte=1, price__gte=0), name="package_values_valid")]

    def __str__(self):
        return f"{self.name} · {self.credits}课时 · ¥{self.price}"


class Purchase(models.Model):
    member = models.ForeignKey(Member, on_delete=models.PROTECT, verbose_name="会员")
    package = models.ForeignKey(Package, on_delete=models.PROTECT, verbose_name="套餐")
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    package_name = models.CharField("购课时套餐名称", max_length=80)
    credits = models.PositiveIntegerField("购买课时")
    amount = models.DecimalField("登记金额（元）", max_digits=10, decimal_places=2)
    purchased_on = models.DateField("购买日期", default=timezone.localdate)
    reason = models.CharField("登记原因", max_length=200)
    request_id = models.UUIDField("请求编号", default=uuid.uuid4, unique=True, editable=False)
    fingerprint = models.CharField(max_length=64, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="操作人")
    created_at = models.DateTimeField("登记时间", auto_now_add=True)

    class Meta:
        verbose_name = "购课记录"
        verbose_name_plural = "购课记录"
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=models.Q(credits__gte=1, amount__gte=0), name="purchase_values_valid")]

    def __str__(self):
        return f"{self.member} · {self.package_name} · {self.credits}课时"


class AvailableSlot(models.Model):
    """由工作人员发布的一对一可预约时间。"""
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    coach = models.ForeignKey(Coach, on_delete=models.PROTECT, verbose_name="教练")
    starts_at = models.DateTimeField("开始时间")
    duration_minutes = models.PositiveSmallIntegerField("时长（分钟）", validators=[MinValueValidator(15), MaxValueValidator(240)])
    active = models.BooleanField("开放预约", default=True)

    class Meta:
        verbose_name = "会员可预约时段"
        verbose_name_plural = "会员可预约时段"
        ordering = ['starts_at', 'pk']
        constraints = [models.CheckConstraint(condition=models.Q(duration_minutes__gte=15, duration_minutes__lte=240), name='slot_duration_valid')]

    def __str__(self):
        return f"{self.course_type} · {self.coach} · {timezone.localtime(self.starts_at):%Y-%m-%d %H:%M}"


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待上课"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "正常取消"
        LEGACY_CANCELLED = "legacy_cancelled", "历史取消（未分类）"
        LATE_CANCELLED = "late_cancelled", "迟取消"
        NO_SHOW = "no_show", "爽约"
        COACH_CANCELLED = "coach_cancelled", "教练取消"
        RESCHEDULED = "rescheduled", "已改期"
        REVERSED = "reversed", "已冲正"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, verbose_name="会员")
    slot = models.ForeignKey(AvailableSlot, null=True, blank=True, on_delete=models.PROTECT, related_name='bookings', verbose_name='发布的预约时段')
    coach = models.ForeignKey(Coach, on_delete=models.PROTECT, verbose_name="教练")
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    course_name = models.CharField("预约时课程名称", max_length=80)
    starts_at = models.DateTimeField("开始时间")
    ends_at = models.DateTimeField("结束时间")
    credits_cost = models.PositiveSmallIntegerField("消耗课时快照")
    status = models.CharField("预约状态", max_length=16, choices=Status.choices, default=Status.PENDING)
    request_id = models.UUIDField("请求编号", default=uuid.uuid4, unique=True, editable=False)
    fingerprint = models.CharField(max_length=64, editable=False)
    previous_booking = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="replacement", verbose_name="原预约")
    replacement_type = models.CharField("后继类型", max_length=16, blank=True, default="",
        choices=[("reschedule", "改期"), ("makeup", "补课")])
    makeup_origin = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="makeup_descendants", verbose_name="补课事项来源")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="创建人")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "课程预约"
        verbose_name_plural = "课程预约"
        ordering = ["starts_at", "pk"]
        permissions = [("complete_booking", "可以确认课程完成并扣课"), ("restore_booking", "可以冲正误扣课时"),
            ("late_cancel_booking", "可以登记迟取消扣课"), ("no_show_booking", "可以登记爽约扣课"),
            ("coach_cancel_booking", "可以登记教练取消"), ("makeup_booking", "可以安排补课"),
            ("backfill_booking", "可以补录历史课程")]
        indexes = [models.Index(fields=["coach", "starts_at"]), models.Index(fields=["member", "starts_at"])]
        constraints = [models.CheckConstraint(condition=models.Q(ends_at__gt=models.F("starts_at"), credits_cost__gte=1), name="booking_values_valid"),
            models.UniqueConstraint(fields=['slot'], condition=models.Q(status__in=['pending', 'completed'], slot__isnull=False), name='one_active_booking_per_slot'),
            models.CheckConstraint(condition=models.Q(status__in=["pending", "completed", "cancelled", "legacy_cancelled", "late_cancelled", "no_show", "coach_cancelled", "rescheduled", "reversed"]), name="booking_status_valid"),
            models.CheckConstraint(condition=(models.Q(previous_booking__isnull=True, replacement_type="") |
                models.Q(previous_booking__isnull=False, replacement_type__in=["reschedule", "makeup"])), name="booking_replacement_valid"),
            models.CheckConstraint(condition=~models.Q(previous_booking=models.F("pk")), name="booking_not_self_previous")]

    def __str__(self):
        return f"{self.member} · {self.course_name}"


class MemberRuleConfirmation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='会员账号')
    member = models.ForeignKey(Member, on_delete=models.PROTECT, verbose_name='会员')
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name='member_confirmations', verbose_name='原预约')
    result_booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.PROTECT, related_name='origin_confirmations', verbose_name='改期后预约')
    action = models.CharField('操作类型', max_length=16, choices=[('cancel','取消'),('reschedule','改期')])
    rule_version = models.CharField('规则版本', max_length=40)
    rule_text = models.TextField('规则文本快照')
    confirmed_at = models.DateTimeField('确认时间')
    request_id = models.UUIDField('请求编号', unique=True)

    class Meta:
        verbose_name = '会员规则确认记录'
        verbose_name_plural = '会员规则确认记录'


class LessonAdjustment(models.Model):
    member = models.ForeignKey(Member, on_delete=models.PROTECT, verbose_name="会员")
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    delta = models.IntegerField("课时调整数量")
    occurred_at = models.DateTimeField("业务实际发生时间")
    reason = models.CharField("调整原因", max_length=200)
    request_id = models.UUIDField("请求编号", default=uuid.uuid4, unique=True, editable=False)
    fingerprint = models.CharField(max_length=64, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="操作人")
    created_at = models.DateTimeField("系统记录时间", auto_now_add=True)

    class Meta:
        verbose_name = "课时调整记录"
        verbose_name_plural = "课时调整记录"
        ordering = ["-created_at", "-pk"]
        constraints = [models.CheckConstraint(condition=~models.Q(delta=0), name="adjustment_nonzero")]

    def __str__(self):
        return f"{self.member} · {self.course_type} · {self.delta:+d}课时"


class LessonEntry(models.Model):
    class Kind(models.TextChoices):
        PURCHASE = "purchase", "购课入账"
        COMPLETE = "complete", "完成扣课"
        LATE_CANCEL = "late_cancel", "迟取消扣课"
        NO_SHOW = "no_show", "爽约扣课"
        RESTORE = "restore", "冲正恢复"
        ADJUSTMENT = "adjustment", "课时补录/调整"

    member = models.ForeignKey(Member, on_delete=models.PROTECT, verbose_name="会员")
    course_type = models.ForeignKey(CourseType, on_delete=models.PROTECT, verbose_name="课程类型")
    kind = models.CharField("类型", max_length=12, choices=Kind.choices)
    delta = models.IntegerField("课时变动")
    balance_before = models.PositiveIntegerField("变动前余额")
    balance_after = models.PositiveIntegerField("变动后余额")
    purchase = models.OneToOneField(Purchase, null=True, blank=True, on_delete=models.PROTECT, verbose_name="关联购课")
    booking = models.ForeignKey(Booking, null=True, blank=True, on_delete=models.PROTECT, verbose_name="关联预约")
    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, verbose_name="原扣课流水")
    adjustment = models.OneToOneField(LessonAdjustment, null=True, blank=True, on_delete=models.PROTECT, verbose_name="关联课时调整")
    reason = models.CharField("原因", max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="操作人")
    created_at = models.DateTimeField("发生时间", auto_now_add=True)
    occurred_at = models.DateTimeField("业务实际发生时间", null=True, blank=True)

    class Meta:
        verbose_name = "课时流水"
        verbose_name_plural = "课时流水"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["booking"], condition=models.Q(kind__in=["complete", "late_cancel", "no_show"]), name="one_consumption_per_booking"),
            models.UniqueConstraint(fields=["booking"], condition=models.Q(kind="restore"), name="one_restore_per_booking"),
            models.CheckConstraint(condition=models.Q(balance_after=models.F("balance_before") + models.F("delta")), name="entry_arithmetic_valid"),
            models.CheckConstraint(condition=(
                models.Q(kind="purchase", delta__gt=0, purchase__isnull=False, booking__isnull=True, reverses__isnull=True, adjustment__isnull=True) |
                models.Q(kind__in=["complete", "late_cancel", "no_show"], delta__lt=0, purchase__isnull=True, booking__isnull=False, reverses__isnull=True, adjustment__isnull=True) |
                models.Q(kind="restore", delta__gt=0, purchase__isnull=True, booking__isnull=False, reverses__isnull=False, adjustment__isnull=True) |
                (models.Q(kind="adjustment", purchase__isnull=True, booking__isnull=True, reverses__isnull=True, adjustment__isnull=False) & ~models.Q(delta=0))), name="entry_links_and_sign_valid"),
        ]


class BookingEvent(models.Model):
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="events", verbose_name="预约")
    action = models.CharField("操作", max_length=16)
    reason = models.CharField("原因", max_length=200)
    details = models.JSONField("变化详情", default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name="操作人")
    created_at = models.DateTimeField("发生时间", auto_now_add=True)
    occurred_at = models.DateTimeField("业务实际发生时间", null=True, blank=True)

    class Meta:
        verbose_name = "预约操作记录"
        verbose_name_plural = "预约操作记录"
        ordering = ["-created_at", "-pk"]


class QuickPackage(models.Model):
    """Stable internal template per course; historical packages remain untouched."""
    course_type = models.OneToOneField(CourseType, on_delete=models.PROTECT)
    package = models.OneToOneField(Package, on_delete=models.PROTECT)


class MemberPhoneIdentity(models.Model):
    phone = models.CharField(max_length=32, unique=True)
    member = models.OneToOneField(Member, on_delete=models.PROTECT)


class QuickOrder(models.Model):
    request_id = models.UUIDField(unique=True)
    fingerprint = models.CharField(max_length=64)
    mode = models.CharField(max_length=5, choices=[('open','开户'),('renew','续课')])
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    member = models.ForeignKey(Member, on_delete=models.PROTECT)
    package = models.ForeignKey(Package, on_delete=models.PROTECT)
    credits = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    purchased_on = models.DateField()
    purchase = models.OneToOneField(Purchase, null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        default_permissions = ()
        constraints = [models.CheckConstraint(condition=models.Q(mode__in=['open','renew'],credits__gte=1,amount__gte=0), name='quick_order_values_valid')]


from .credential_models import CredentialState  # noqa: E402,F401
