from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from studio.forms import MemberForm
from studio.models import Booking, Coach, CourseType, LessonEntry, Member, MemberAccount, Package, Purchase


class ArchiveTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('archive_admin', password='FitStudio-Test-Only-Archive-Admin!')
        self.member = Member.objects.create(name='归档会员', phone='00000001111')
        self.user = get_user_model().objects.create_user('archive_member', password='FitStudio-Test-Only-Archive-Member!')
        self.binding = MemberAccount.objects.create(member=self.member, user=self.user)

    def archive(self, **kwargs):
        from studio.archive_services import archive_member
        data = dict(member_id=self.member.pk, actor=self.admin, reason='离店归档')
        data.update(kwargs)
        return archive_member(**data)

    def restore(self, **kwargs):
        from studio.archive_services import restore_member
        data = dict(member_id=self.member.pk, actor=self.admin, reason='会员返店')
        data.update(kwargs)
        return restore_member(**data)

    def test_status_is_explicitly_available(self):
        self.assertIn(('archived', '已归档'), Member.Status.choices)

    def test_archive_and_restore_preserve_binding_password_and_prior_activity(self):
        from studio.models import MemberArchiveEvent
        original_password = self.user.password
        self.user.is_active = False
        self.user.save()
        self.member.status = Member.Status.INACTIVE
        self.member.save()
        event = self.archive()
        self.member.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(self.member.status, 'archived')
        self.assertFalse(self.user.is_active)
        self.assertEqual(event.previous_status, 'inactive')
        self.assertFalse(event.previous_user_active)
        self.assertEqual(event.account_user_id, self.user.pk)
        self.assertEqual(event.created_by, self.admin)
        self.assertEqual(event.reason, '离店归档')
        self.assertIsNotNone(event.created_at)
        self.assertEqual(self.archive().pk, event.pk)
        self.assertEqual(MemberArchiveEvent.objects.count(), 1)
        self.restore()
        self.member.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(self.member.status, 'active')
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.password, original_password)
        self.assertEqual(MemberAccount.objects.get().pk, self.binding.pk)
        self.assertEqual(MemberArchiveEvent.objects.count(), 2)
        self.restore()
        self.assertEqual(MemberArchiveEvent.objects.count(), 2)

    def test_active_user_restores_and_new_archive_cycle_uses_latest_snapshot(self):
        self.archive(); self.restore()
        self.user.refresh_from_db(); self.assertTrue(self.user.is_active)
        self.user.is_active = False; self.user.save()
        self.archive(); self.restore()
        self.user.refresh_from_db(); self.assertFalse(self.user.is_active)

    def test_archive_preserves_purchase_ledger_and_balances_without_new_credits(self):
        import uuid
        from studio.services import register_purchase, credit_balance
        course = CourseType.objects.create(name='历史课程')
        package = Package.objects.create(name='旧套餐', course_type=course, credits=8, price=500)
        purchase = register_purchase(member_id=self.member.pk, package_id=package.pk,
            purchased_on=timezone.localdate(), actor=self.admin, request_id=uuid.uuid4(), reason='原购课')
        before_purchase = list(Purchase.objects.values())
        before_entries = list(LessonEntry.objects.values())
        self.archive(); self.restore()
        self.assertEqual(list(Purchase.objects.values()), before_purchase)
        self.assertEqual(list(LessonEntry.objects.values()), before_entries)
        self.assertEqual(credit_balance(self.member.pk, course.pk), 8)
        self.assertEqual(Purchase.objects.get().pk, purchase.pk)

    def test_unbound_member_roundtrip_does_not_create_account(self):
        member = Member.objects.create(name='无账号会员')
        self.archive(member_id=member.pk); self.restore(member_id=member.pk)
        member.refresh_from_db(); self.assertEqual(member.status, 'active')
        self.assertFalse(MemberAccount.objects.filter(member=member).exists())

    def test_group_permission_and_inactive_staff_cannot_archive(self):
        permission = Permission.objects.get(codename='archive_member')
        coach = get_user_model().objects.create_user('group_coach', is_staff=True)
        group = Group.objects.create(name='日常教练')
        group.permissions.add(permission); coach.groups.add(group)
        self.archive(actor=coach)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'archived')
        self.restore(actor=coach)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'active')
        self.user.refresh_from_db(); self.assertTrue(self.user.is_active)
        coach.is_active = False
        with self.assertRaises(PermissionDenied): self.archive(actor=coach)

    def test_pending_bookings_require_acknowledgement_and_remain_unchanged(self):
        from studio.services import BusinessError
        course = CourseType.objects.create(name='私教')
        coach = Coach.objects.create(name='教练')
        start = timezone.now() + timedelta(days=1)
        booking = Booking.objects.create(member=self.member, coach=coach, course_type=course,
            course_name='私教', starts_at=start, ends_at=start+timedelta(hours=1),
            credits_cost=1, created_by=self.admin)
        with self.assertRaises(BusinessError):
            self.archive()
        self.member.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(self.member.status, 'active'); self.assertTrue(self.user.is_active)
        self.archive(acknowledge_bookings=True)
        booking.refresh_from_db(); self.assertEqual(booking.status, 'pending')

    def test_coach_and_nonstaff_permission_holder_cannot_archive(self):
        coach = get_user_model().objects.create_user('archive_coach', is_staff=True)
        with self.assertRaises(PermissionDenied):
            self.archive(actor=coach)
        permission = Permission.objects.get(codename='archive_member')
        coach.user_permissions.add(permission)
        coach = get_user_model().objects.get(pk=coach.pk)
        self.archive(actor=coach)
        self.restore(actor=coach)
        coach.is_staff = False; coach.save()
        with self.assertRaises(PermissionDenied):
            self.archive(actor=coach)

    def test_optional_reason_and_audit_cannot_be_changed_or_deleted(self):
        from studio.services import BusinessError
        with self.assertRaises(BusinessError): self.archive(reason='长' * 201)
        event = self.archive(reason=' ')
        self.assertEqual(event.reason, '')
        event.reason = '改写'
        with self.assertRaises(ValidationError): event.save()
        with self.assertRaises(ValidationError): event.delete()
        with self.assertRaises(ValidationError): type(event).objects.filter(pk=event.pk).update(reason='改写')
        with self.assertRaises(ValidationError): type(event).objects.filter(pk=event.pk).delete()

    def test_archive_confirmation_accepts_optional_reason(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/members/{self.member.pk}/archive/', {'confirm':'on'})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'archived')
        response = self.client.post(f'/members/{self.member.pk}/restore/', {'confirm':'on'})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'active')

    def test_archived_member_hidden_from_coach_list_and_direct_archive_urls(self):
        self.archive()
        coach = get_user_model().objects.create_user('archive_list_coach', is_staff=True)
        coach.user_permissions.add(*Permission.objects.filter(content_type__app_label='studio',
            codename__in=['view_member', 'change_member', 'view_lessonentry']))
        self.client.force_login(coach)
        for query in ('', '?q=00000001111', '?status=archived'):
            response = self.client.get('/members/' + query)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.member.name)
            self.assertEqual(list(response.context['object_list']), [])
        for path in ('/members/archived/', f'/members/{self.member.pk}/archive/',
                     f'/members/{self.member.pk}/archived/', f'/members/{self.member.pk}/restore/'):
            self.assertEqual(self.client.get(path).status_code, 403)
        for path in (f'/members/{self.member.pk}/archive/', f'/members/{self.member.pk}/restore/'):
            self.assertEqual(self.client.post(path, {'confirm':'on'}).status_code, 403)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'archived')

    def test_regular_member_form_cannot_archive_or_edit_archived_member(self):
        data = dict(name='会员', phone='1', status='archived', joined_on='2026-11-08', notes='')
        self.assertFalse(MemberForm(data=data).is_valid())
        self.archive()
        self.member.refresh_from_db()
        data['status'] = 'active'
        self.assertFalse(MemberForm(data=data, instance=self.member).is_valid())

    def test_pages_authorization_confirmation_and_restore_post(self):
        self.client.force_login(self.admin)
        response = self.client.get(f'/members/{self.member.pk}/archive/')
        self.assertContains(response, self.member.phone)
        self.assertContains(response, '归档')
        self.assertEqual(self.client.post(f'/members/{self.member.pk}/archive/', {'reason':'离店'}).status_code, 200)
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'active')
        self.client.post(f'/members/{self.member.pk}/archive/', {'reason':'离店', 'confirm':'on'})
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'archived')
        self.assertContains(self.client.get('/members/archived/'), self.member.name)
        self.assertContains(self.client.get(f'/members/{self.member.pk}/archived/'), '归档操作记录')
        self.client.get(f'/members/{self.member.pk}/restore/')
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'archived')
        self.client.post(f'/members/{self.member.pk}/restore/', {'reason':'返店', 'confirm':'on'})
        self.member.refresh_from_db(); self.assertEqual(self.member.status, 'active')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/members/archived/').status_code, 403)
        self.assertEqual(self.client.get(f'/members/{self.member.pk}/archive/').status_code, 403)
