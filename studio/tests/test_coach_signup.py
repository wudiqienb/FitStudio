from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.models import LogEntry
from django.db import IntegrityError
from django.test import Client, TestCase

from studio.models import Member, MemberAccount


class CoachSignupTests(TestCase):
    def setUp(self):
        self.coach = get_user_model().objects.create_user('coach', password='FitStudio-Test-Only-Coach!', is_staff=True, is_superuser=True)
        self.coach.user_permissions.add(*Permission.objects.filter(codename__in=['view_member', 'add_member', 'add_memberaccount']))
        self.client.force_login(self.coach)
        self.data = dict(name='测试会员', status='active', joined_on='2026-11-02',
                         open_account='on', username='member900', password1='FitStudio-Test-Only-Signup!', password2='FitStudio-Test-Only-Signup!')
        self.member = Member.objects.create(name='已有会员')
        self.url = f'/members/{self.member.pk}/account/add/'

    def test_create_member_and_account(self):
        response = self.client.post('/members/add/', self.data)
        self.assertEqual(response.status_code, 302)
        account = MemberAccount.objects.get(user__username='member900')
        self.assertEqual(account.member.name, '测试会员')
        self.assertTrue(account.user.check_password(self.data['password1']))
        self.assertFalse(account.user.is_staff or account.user.is_superuser)
        self.assertFalse(account.user.groups.exists() or account.user.user_permissions.exists())

    def test_existing_member_open_and_portal_login(self):
        self.assertEqual(self.client.post(self.url, self.data).status_code, 302)
        client = Client()
        result = client.post('/portal/login/', {'username':'member900', 'password':self.data['password1']})
        self.assertEqual(result.status_code, 302)
        self.assertContains(client.get('/portal/'), self.member.name)

    def test_invalid_password_retains_no_partial_member(self):
        self.data['password2'] = 'different'
        self.assertEqual(self.client.post('/members/add/', self.data).status_code, 200)
        self.assertFalse(Member.objects.filter(name='测试会员').exists())
        self.assertFalse(get_user_model().objects.filter(username='member900').exists())

    def test_weak_password_rejected(self):
        self.data.update(password1='12345678', password2='12345678')
        self.assertEqual(self.client.post(self.url, self.data).status_code, 200)
        self.assertFalse(MemberAccount.objects.exists())

    def test_existing_username_not_modified(self):
        original = self.coach.password
        self.data['username'] = 'coach'
        self.assertEqual(self.client.post('/members/add/', self.data).status_code, 200)
        self.coach.refresh_from_db()
        self.assertEqual(self.coach.password, original)
        self.assertFalse(Member.objects.filter(name='测试会员').exists())

    def test_replay_creates_only_once(self):
        self.client.post('/members/add/', self.data)
        self.client.post('/members/add/', self.data)
        self.assertEqual(Member.objects.filter(name='测试会员').count(), 1)
        self.assertEqual(MemberAccount.objects.count(), 1)

    def test_bound_member_cannot_be_rebound(self):
        self.client.post(self.url, self.data)
        self.data['username'] = 'another900'
        self.client.post(self.url, self.data)
        self.assertFalse(get_user_model().objects.filter(username='another900').exists())
        self.assertEqual(MemberAccount.objects.count(), 1)

    def test_cannot_inject_staff_or_permissions(self):
        self.data.update(is_staff='on', is_superuser='on', groups=['1'], user_permissions=['1'])
        self.client.post(self.url, self.data)
        user = get_user_model().objects.get(username='member900')
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertFalse(user.groups.exists() or user.user_permissions.exists())

    def test_requires_opening_permission(self):
        self.coach.is_superuser = False
        self.coach.save(update_fields=['is_superuser'])
        self.coach.user_permissions.remove(Permission.objects.get(codename='add_memberaccount'))
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.assertEqual(self.client.post('/members/add/', self.data).status_code, 403)
        self.assertFalse(Member.objects.filter(name='测试会员').exists())

    def test_nonstaff_and_anonymous_denied(self):
        user = get_user_model().objects.create_user('ordinary')
        user.user_permissions.set(self.coach.user_permissions.all())
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, self.data).status_code, 302)

    def test_binding_failure_rolls_back_whole_creation(self):
        with patch('studio.models.MemberAccount.save', side_effect=IntegrityError('simulated conflict')):
            response = self.client.post('/members/add/', self.data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Member.objects.filter(name='测试会员').exists())
        self.assertFalse(get_user_model().objects.filter(username='member900').exists())

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coach)
        self.assertEqual(client.post(self.url, self.data).status_code, 403)

    def test_form_and_card_entry(self):
        self.assertContains(self.client.get('/members/add/'), '同时开通会员账号')
        self.assertContains(self.client.get('/members/'), self.url)
        self.client.post(self.url, self.data)
        response = self.client.get('/members/')
        self.assertContains(response, 'member900')
        self.assertNotContains(response, self.url)

    def test_audit_contains_no_password(self):
        self.client.post(self.url, self.data)
        self.assertTrue(LogEntry.objects.filter(user=self.coach).exists())
        self.assertFalse(LogEntry.objects.filter(change_message__contains=self.data['password1']).exists())

    def test_account_optional(self):
        self.data.pop('open_account')
        self.client.post('/members/add/', self.data)
        self.assertTrue(Member.objects.filter(name='测试会员').exists())
        self.assertFalse(MemberAccount.objects.exists())

    def test_password_redacted_on_failed_forms(self):
        self.data['password2'] = 'FitStudio-Test-Only-Unmatched!'
        for url in ('/members/add/', self.url):
            response = self.client.post(url, self.data)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, self.data['password1'])
            self.assertNotContains(response, self.data['password2'])
            self.assertIn('password1', response.wsgi_request.sensitive_post_parameters)
