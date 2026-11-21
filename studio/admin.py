from django.contrib import admin


class AdministratorOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request): return request.user.is_active and request.user.is_superuser
    def has_view_permission(self, request, obj=None): return self.has_module_permission(request)
    def has_add_permission(self, request): return self.has_module_permission(request)
    def has_change_permission(self, request, obj=None): return self.has_module_permission(request)
    def has_delete_permission(self, request, obj=None): return self.has_module_permission(request)


from .models import MemberAccount, AvailableSlot, MemberRuleConfirmation


@admin.register(MemberAccount)
class MemberAccountAdmin(AdministratorOnlyAdmin):
    def has_delete_permission(self, request, obj=None): return False

    list_display = ("member", "user", "created_at")
    search_fields = ("member__name", "user__username")
    list_select_related = ("member", "user")
    readonly_fields = ("created_at", "reset_credentials_link")

    @admin.display(description="登录凭据")
    def reset_credentials_link(self, obj):
        if not obj or not obj.pk: return "保存绑定后可重置"
        from django.utils.html import format_html
        from django.urls import reverse
        return format_html('<a href="{}">核对身份并重置凭据</a>', reverse('studio:member_credentials_reset', args=[obj.member_id]))


    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            from django.contrib.auth import get_user_model
            kwargs["queryset"] = get_user_model().objects.filter(is_staff=False, is_superuser=False)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(AvailableSlot)
class AvailableSlotAdmin(admin.ModelAdmin):
    list_display = ('course_type', 'coach', 'starts_at', 'duration_minutes', 'active')
    list_filter = ('active', 'course_type', 'coach')

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.bookings.exists():
            return ('course_type', 'coach', 'starts_at', 'duration_minutes')
        return ()


@admin.register(MemberRuleConfirmation)
class MemberRuleConfirmationAdmin(admin.ModelAdmin):
    list_display = ('member', 'booking', 'action', 'rule_version', 'confirmed_at')
    readonly_fields = ('user', 'member', 'booking', 'result_booking', 'action', 'rule_version', 'rule_text', 'confirmed_at', 'request_id')
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False

from .models import Booking, BookingEvent, Coach, CourseType, LessonEntry, Member, Package, Purchase, LessonAdjustment


admin.site.site_header = "FitStudio 工作室管理"
admin.site.site_title = "FitStudio 管理后台"
admin.site.index_title = "工作室档案管理"


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    from .forms import MemberForm
    form = MemberForm

    def has_delete_permission(self, request, obj=None): return False

    def get_queryset(self, request):
        from .archive_services import can_archive_member
        qs = super().get_queryset(request)
        return qs if can_archive_member(request.user) else qs.exclude(status=Member.Status.ARCHIVED)

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.status == Member.Status.ARCHIVED: return False
        return super().has_change_permission(request, obj)

    list_display = ("name", "phone", "status", "joined_on")
    search_fields = ("name", "phone")
    list_filter = ("status", "joined_on")
    ordering = ("-created_at", "-pk")
    list_per_page = 20
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("基本资料", {"fields": ("name", "phone", "status", "joined_on")}),
        ("补充信息", {"fields": ("notes",)}),
        ("记录时间", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(Coach)
class CoachAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "specialty", "status")
    search_fields = ("name", "phone", "specialty")
    list_filter = ("status",)
    ordering = ("-created_at", "-pk")
    list_per_page = 20
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("基本资料", {"fields": ("name", "phone", "specialty", "status")}),
        ("补充信息", {"fields": ("notes",)}),
        ("记录时间", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(CourseType)
class CourseTypeAdmin(AdministratorOnlyAdmin):
    list_display = ("name", "duration_minutes", "credits_cost", "active")
    search_fields = ("name",)
    list_filter = ("active",)


@admin.register(Package)
class PackageAdmin(AdministratorOnlyAdmin):
    list_display = ("name", "course_type", "credits", "price", "active")
    list_filter = ("course_type", "active")
    search_fields = ("name",)


class ReadOnlyHistoryAdmin(admin.ModelAdmin):
    """业务状态和流水只允许从统一服务入口变更。"""
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Purchase)
class PurchaseAdmin(ReadOnlyHistoryAdmin):
    list_display = ("member", "package_name", "credits", "amount", "purchased_on", "created_by")
    search_fields = ("member__name", "package_name")


@admin.register(Booking)
class BookingAdmin(ReadOnlyHistoryAdmin):
    list_display = ("member", "coach", "course_name", "starts_at", "status")
    list_filter = ("status", "course_type")
    search_fields = ("member__name", "coach__name")


@admin.register(LessonEntry)
class LessonEntryAdmin(ReadOnlyHistoryAdmin):
    list_display = ("member", "course_type", "kind", "delta", "balance_before", "balance_after", "created_at")
    list_filter = ("kind", "course_type")
    search_fields = ("member__name", "reason")


@admin.register(BookingEvent)
class BookingEventAdmin(ReadOnlyHistoryAdmin):
    list_display = ("booking", "action", "reason", "created_by", "created_at")


@admin.register(LessonAdjustment)
class LessonAdjustmentAdmin(ReadOnlyHistoryAdmin):
    list_display = ("member", "course_type", "delta", "occurred_at", "created_by", "created_at")

# User/group administration is never delegated to coaches by model permissions.
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.admin import UserAdmin, GroupAdmin
admin.site.unregister(get_user_model())
admin.site.unregister(Group)
@admin.register(get_user_model())
class AdministratorUserAdmin(AdministratorOnlyAdmin, UserAdmin):
    pass
@admin.register(Group)
class AdministratorGroupAdmin(AdministratorOnlyAdmin, GroupAdmin):
    pass
