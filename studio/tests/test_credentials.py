import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, override_settings

from studio.models import Member, MemberAccount, Purchase
from studio.quick_services import quick_open, reset_member_credentials
from studio.services import BusinessError
from studio.tests.test_quick import QuickFixtures


class CredentialTests(QuickFixtures, TestCase):
    def test_name_username_and_readable_temporary_password(self):
        order, password = self.open(name='张三')
        user = order.member.login_account.user
        self.assertEqual(user.username, '张三')
        self.assertEqual(len(password), 10)
        self.assertFalse(set(password) & set('O0I1l'))
        self.assertTrue(user.check_password(password))

    def test_english_name_and_member_name_collision_suffix(self):
        order,_=self.open(name='Alice',phone='00000101001')
        self.assertEqual(order.member.login_account.user.username,'Alice')
        second,_=self.open(name='Alice',phone='00000101002')
        self.assertEqual(second.member.login_account.user.username,'Alice2')
        self.assertEqual(order.member.login_account.user.username,'Alice')

    def test_collision_including_staff_never_reuses_account(self):
        staff = get_user_model().objects.create_user('张三', password='FitStudio-Test-Only-Preserved!', is_staff=True)
        old_hash = staff.password
        get_user_model().objects.create_user('张三2')
        order, _ = self.open(name='张三')
        self.assertEqual(order.member.login_account.user.username, '张三3')
        staff.refresh_from_db()
        self.assertEqual(staff.password, old_hash)
        self.assertFalse(MemberAccount.objects.filter(user=staff).exists())

    def test_normalized_name_and_invalid_name_roll_back(self):
        order, _ = self.open(name=' Ａ丽 ')
        self.assertEqual(order.member.name, 'A丽')
        self.assertEqual(order.member.login_account.user.username, 'A丽')
        for name in ['张 三', '张/三', '\u200b张三']:
            with self.subTest(name=name), self.assertRaises(BusinessError):
                self.open(name=name, phone='00000404321')
        self.assertEqual(Purchase.objects.count(), 1)

    def test_temporary_login_gates_all_portal_pages_and_services(self):
        from studio.portal_access import portal_member_for
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client()
        response = portal.post('/portal/login/', {'username': user.username, 'password': password})
        self.assertRedirects(response, '/portal/change-password/', fetch_redirect_response=False)
        for path in ['/portal/', '/portal/courses/', '/portal/bookings/', '/portal/bookings/999/cancel/']:
            with self.subTest(path=path):
                self.assertRedirects(portal.get(path), '/portal/change-password/', fetch_redirect_response=False)
        self.assertRedirects(portal.post('/portal/courses/999/book/', {}), '/portal/change-password/', fetch_redirect_response=False)
        with self.assertRaises(PermissionDenied):
            portal_member_for(user)
        from studio.portal_services import member_book
        with self.assertRaises(PermissionDenied):
            member_book(actor=user, slot_id=999, expected_slot='invalid', request_id=uuid.uuid4())

    def test_change_validates_password_and_preserves_authenticated_session(self):
        from studio.credential_models import CredentialState
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client()
        portal.login(username=user.username, password=password)
        for old, replacement, confirmation in [(password, '12345678', '12345678'), (password, 'FitStudio-Test-Only-Replacement!', 'different'), ('FitStudio-Test-Only-Wrong-Old!', 'FitStudio-Test-Only-Replacement!', 'FitStudio-Test-Only-Replacement!')]:
            response = portal.post('/portal/change-password/', {'old_password': old, 'new_password1': replacement, 'new_password2': confirmation})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(CredentialState.objects.get(user=user).required_change)
            user.refresh_from_db()
            self.assertTrue(user.check_password(password))
        response = portal.post('/portal/change-password/', {'old_password': password, 'new_password1': 'FitStudio-Test-Only-Replacement!', 'new_password2': 'FitStudio-Test-Only-Replacement!'})
        self.assertRedirects(response, '/portal/')
        user.refresh_from_db()
        self.assertTrue(user.check_password('FitStudio-Test-Only-Replacement!'))
        self.assertFalse(user.check_password(password))
        self.assertFalse(CredentialState.objects.get(user=user).required_change)
        self.assertEqual(portal.get('/portal/').status_code, 200)

    def test_existing_account_without_state_is_unchanged(self):
        user = get_user_model().objects.create_user('legacy', password='FitStudio-Test-Only-Legacy!')
        MemberAccount.objects.create(member=self.member, user=user)
        portal = Client()
        portal.login(username=user.username, password='FitStudio-Test-Only-Legacy!')
        self.assertEqual(portal.get('/portal/').status_code, 200)

    def test_explicit_reset_permission_and_no_binding_changes(self):
        from studio.credential_models import CredentialState
        order, old = self.open()
        user = order.member.login_account.user
        with self.assertRaises(PermissionDenied):
            reset_member_credentials(actor=self.actor, member_id=order.member_id)
        self.actor.user_permissions.add(Permission.objects.get(codename='reset_member_credentials'))
        binding, password = reset_member_credentials(actor=self.actor, member_id=order.member_id)
        self.assertEqual(binding.user_id, user.pk)
        self.assertTrue(CredentialState.objects.get(user=user).required_change)
        user.refresh_from_db()
        self.assertTrue(user.check_password(password))
        self.assertFalse(user.check_password(old))
        self.assertEqual(MemberAccount.objects.count(), 1)

    def test_reset_invalidates_old_member_session(self):
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client()
        portal.login(username=user.username, password=password)
        admin = get_user_model().objects.create_superuser('session_reset_admin', password=None)
        reset_member_credentials(actor=admin, member_id=order.member_id)
        self.assertRedirects(portal.get('/portal/'), '/portal/login/?next=/portal/', fetch_redirect_response=False)

    def test_archived_member_cannot_reset_or_change_password(self):
        from studio.credential_models import CredentialState
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client()
        portal.force_login(user)
        order.member.status = 'archived'
        order.member.save(update_fields=['status'])
        admin = get_user_model().objects.create_superuser('archive_reset_admin', password=None)
        with self.assertRaises(BusinessError):
            reset_member_credentials(actor=admin, member_id=order.member_id)
        self.assertEqual(portal.get('/portal/change-password/').status_code, 403)
        response = portal.post('/portal/change-password/', {'old_password': password, 'new_password1': 'FitStudio-Test-Only-Replacement!', 'new_password2': 'FitStudio-Test-Only-Replacement!'})
        self.assertEqual(response.status_code, 403)
        user.refresh_from_db()
        self.assertTrue(user.check_password(password))
        self.assertTrue(CredentialState.objects.get(user=user).required_change)

    def test_inactive_member_or_user_cannot_change_password(self):
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client()
        portal.force_login(user)
        order.member.status = 'inactive'
        order.member.save(update_fields=['status'])
        self.assertEqual(portal.get('/portal/change-password/').status_code, 403)
        order.member.status = 'active'
        order.member.save(update_fields=['status'])
        user.is_active = False
        user.save(update_fields=['is_active'])
        self.assertRedirects(portal.get('/portal/change-password/'), '/portal/login/?next=/portal/change-password/', fetch_redirect_response=False)

    def test_change_password_csrf_and_staff_are_rejected(self):
        order, password = self.open()
        user = order.member.login_account.user
        portal = Client(enforce_csrf_checks=True)
        portal.force_login(user)
        response = portal.post('/portal/change-password/', {'old_password': password, 'new_password1': 'FitStudio-Test-Only-Replacement!', 'new_password2': 'FitStudio-Test-Only-Replacement!'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/portal/change-password/').status_code, 403)

    def test_receipt_secret_only_once_and_copy_controls(self):
        data = dict(name='复制会员', phone='00000608888', credits=3, amount='100.00', request_id=str(uuid.uuid4()))
        response = self.client.post('/quick/open/', data)
        self.assertContains(response, '复制账号')
        self.assertContains(response, '复制密码')
        self.assertContains(response, '复制完整登录信息')
        self.assertIn('no-store', response['Cache-Control'])
        password = response.context['password']
        self.assertNotIn(password, str(dict(self.client.session)))
        self.assertNotContains(self.client.post('/quick/open/', data), password)
