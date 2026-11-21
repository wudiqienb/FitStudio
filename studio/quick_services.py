"""快速开户和续课：权限、身份、幂等及购课共用一个写事务。"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
from django.core.exceptions import PermissionDenied
from django.db import transaction, IntegrityError, OperationalError, connection
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .models import (Member, MemberAccount, CourseType, Package, Purchase,
                     QuickOrder, QuickPackage, MemberPhoneIdentity)
from .services import BusinessError, _authorize, _fingerprint, _request_id, register_purchase
from .credential_services import username_for_name, temporary_password, require_password_change, authorize_reset


def configured_course():
    value = getattr(settings, 'FITSTUDIO_QUICK_COURSE_TYPE_ID', None)
    try:
        if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
            raise ValueError
        course = CourseType.objects.get(pk=int(value), active=True)
    except (ValueError, TypeError, OverflowError, CourseType.DoesNotExist):
        raise BusinessError('未配置有效且启用的购课课程类型，请联系管理员。') from None
    return course


def normalize_phone(value):
    raw = unicodedata.normalize('NFKC', str(value or '')).strip()
    raw = re.sub(r'[\s()（）-]', '', raw)
    if raw.startswith('+86'): raw = raw[3:]
    elif raw.startswith('0086'): raw = raw[4:]
    if not re.fullmatch(r'[0-9]{5,32}', raw):
        raise BusinessError('请填写有效联系电话（5～32位数字，可含空格、横线或+86区号）。')
    return raw


def _values(credits, amount):
    try:
        value = Decimal(str(amount))
        valid = value.is_finite() and 0 <= value <= Decimal('99999999.99') and value == value.quantize(Decimal('.01'))
    except (InvalidOperation, ValueError):
        valid = False
    if type(credits) is not int or not 1 <= credits <= 2147483647 or not valid:
        raise BusinessError('购买课时须为正整数；实收金额必填，非负、最多两位小数且不超过99999999.99元。')
    return credits, value.quantize(Decimal('.01'))


def _template(course):
    mapping = QuickPackage.objects.select_related('package').filter(course_type=course).first()
    if mapping:
        package = mapping.package
        if package.course_type_id != course.pk or not package.active or package.name != '私教课':
            raise BusinessError('内部私教课模板已变更或停用，请联系管理员。')
        return package
    # Do not rename, deduplicate or otherwise mutate historical packages.
    package = Package.objects.filter(course_type=course, name='私教课', active=True).order_by('pk').first()
    if package is None:
        package = Package.objects.create(course_type=course, name='私教课', credits=1, price=Decimal('0.00'))
    QuickPackage.objects.create(course_type=course, package=package)
    return package


@sensitive_variables('password')
def _credentials(user):
    return temporary_password(user)


@sensitive_variables('password')
def _execute(mode, *, actor, request_id, credits, amount, name=None, phone=None, member_id=None):
    try:
        with transaction.atomic():
            actor = get_user_model().objects.get(pk=actor.pk)
            _authorize(actor, 'quick_open_member' if mode == 'open' else 'quick_renew_member')
            course = configured_course()
            credits, amount = _values(credits, amount)
            key = _request_id(request_id)
            if mode == 'open':
                name = str(name or '').strip()
                if not name or len(name) > 80: raise BusinessError('请填写姓名（1～80字）。')
                name = get_user_model().normalize_username(name)
                name, _ = username_for_name(name)
                phone = normalize_phone(phone)
            fingerprint = _fingerprint(dict(mode=mode, actor=actor.pk, course=course.pk, name=name,
                                            phone=phone, member=member_id, credits=credits, amount=str(amount)))
            old = QuickOrder.objects.select_related('purchase').filter(request_id=key).first()
            if old:
                if old.fingerprint != fingerprint or old.purchase_id is None:
                    raise BusinessError('请求编号已用于不同内容，请重新打开表单。')
                return old, None
            password = None
            if mode == 'open':
                conflict = MemberPhoneIdentity.objects.filter(phone=phone).exists()
                # Includes inactive and historical duplicates without migrating or merging them.
                for old_phone in Member.objects.exclude(phone='').values_list('phone', flat=True):
                    try: conflict = conflict or normalize_phone(old_phone) == phone
                    except BusinessError: pass
                if conflict:
                    raise BusinessError('该联系电话已有会员或身份占用，请搜索核对已有会员；续课请使用老会员续课，资料冲突请联系管理员。')
                member = Member(name=name, phone=phone)
                member.full_clean(); member.save()
                MemberPhoneIdentity.objects.create(phone=phone, member=member)
            else:
                try: member = Member.objects.get(pk=member_id)
                except Member.DoesNotExist: raise BusinessError('请选择已有会员。') from None
                if member.status != Member.Status.ACTIVE:
                    raise BusinessError('该会员已停用，不能续课；请联系管理员核对，不会自动激活。')
            package = _template(course)
            order = QuickOrder.objects.create(request_id=key, fingerprint=fingerprint, mode=mode,
                actor=actor, member=member, package=package, credits=credits, amount=amount,
                purchased_on=timezone.localdate())
            purchase = register_purchase(member_id=member.pk, package_id=package.pk,
                credits=credits, amount=amount, purchased_on=order.purchased_on,
                reason='快速开通购课' if mode == 'open' else '老会员续课', actor=actor,
                request_id=key, quick_order_id=order.pk)
            if mode == 'open':
                _, username = username_for_name(name)
                user = get_user_model()(username=username, is_active=True,
                                        is_staff=False, is_superuser=False)
                password = _credentials(user); user.full_clean(); user.save()
                MemberAccount.objects.create(member=member, user=user)
                require_password_change(user)
            order.purchase = purchase; order.save(update_fields=['purchase'])
            LogEntry.objects.log_actions(actor.pk, [order], ADDITION,
                change_message='快速新增会员并购课开户' if mode == 'open' else '老会员续课')
            return order, password
    except IntegrityError as error:
        raise BusinessError('身份、账号或请求发生冲突；本次未提交，请刷新核对已有会员。') from error
    except OperationalError as error:
        if 'locked' in str(error).lower() or 'busy' in str(error).lower():
            raise BusinessError('系统忙，本次未提交，请使用原请求重试。') from error
        raise


def quick_open(**kwargs):
    return _execute('open', **kwargs)


def quick_renew(**kwargs):
    return _execute('renew', **kwargs)[0]


def authorize_quick_purchase(actor, values):
    """No broad add_purchase permission: consume only the pending authorized order."""
    if not connection.in_atomic_block: raise PermissionDenied
    order = QuickOrder.objects.filter(pk=values.get('quick_order_id'), actor=actor, purchase__isnull=True).first()
    if order is None: raise PermissionDenied
    _authorize(actor, 'quick_open_member' if order.mode == 'open' else 'quick_renew_member')
    expected = dict(member_id=order.member_id, package_id=order.package_id, credits=order.credits,
                    amount=order.amount, purchased_on=order.purchased_on, request_id=order.request_id,
                    reason='快速开通购课' if order.mode == 'open' else '老会员续课')
    if any(values.get(k) != v for k,v in expected.items()) or values.get('occurred_at') is not None:
        raise PermissionDenied
    mapping = QuickPackage.objects.filter(course_type=configured_course(), package_id=order.package_id).exists()
    if not mapping: raise PermissionDenied


@sensitive_variables('password')
def reset_member_credentials(*, actor, member_id):
    with transaction.atomic():
        actor = get_user_model().objects.get(pk=actor.pk)
        authorize_reset(actor)
        try:
            binding = MemberAccount.objects.select_related('user', 'member').get(member_id=member_id)
        except MemberAccount.DoesNotExist: raise BusinessError('会员尚未绑定账号，不能重置。') from None
        if binding.member.status == Member.Status.ARCHIVED:
            raise BusinessError('会员已归档，不能重置登录凭据。')
        user = get_user_model().objects.select_for_update().get(pk=binding.user_id)
        if user.is_staff or user.is_superuser or not user.is_active: raise BusinessError('账号状态异常，请核对。')
        password = _credentials(user); user.save(update_fields=['password'])
        require_password_change(user)
        LogEntry.objects.log_actions(actor.pk, [binding], CHANGE, change_message='管理员重置会员登录凭据')
        return binding, password
