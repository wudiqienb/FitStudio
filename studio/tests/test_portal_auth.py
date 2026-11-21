import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import Client, TestCase
from django.urls import reverse

from studio.models import Member


class PortalEntryTests(TestCase):
    def test_member_login_page_exists(self):
        self.assertEqual(self.client.get('/portal/login/').status_code, 200)


class PortalAccountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from studio.models import MemberAccount
        cls.password = secrets.token_urlsafe(24)
        User = get_user_model()
        cls.user = User.objects.create_user('fictional_portal_member', password=cls.password)
        cls.other_user = User.objects.create_user('fictional_portal_other', password=None)
        cls.unbound = User.objects.create_user('fictional_portal_unbound', password=cls.password)
        cls.staff = User.objects.create_user('fictional_portal_staff', password=cls.password, is_staff=True)
        cls.admin = User.objects.create_superuser('fictional_portal_admin', password=cls.password)
        cls.member = Member.objects.create(name='虚构会员甲', notes='不应展示的内部备注')
        cls.other = Member.objects.create(name='虚构会员乙')
        cls.binding = MemberAccount.objects.create(user=cls.user, member=cls.member)
        MemberAccount.objects.create(user=cls.other_user, member=cls.other)
        cls.staff.user_permissions.add(Permission.objects.get(codename='view_member'))

    def login(self, username=None, password=None, **extra):
        return self.client.post('/portal/login/', dict(
            username=username or self.user.username,
            password=password or self.password, **extra))

    def test_normal_member_login_and_minimal_home(self):
        response = self.login()
        self.assertRedirects(response, '/portal/')
        self.assertEqual(int(self.client.session['_auth_user_id']), self.user.pk)
        self.assertNotEqual(self.user.password, self.password)
        self.assertTrue(self.user.check_password(self.password))
        home = self.client.get('/portal/')
        self.assertContains(home, self.member.name)
        self.assertContains(home, '欢迎回来')
        self.assertContains(home, '会员中心')
        for text in ('改期', '取消', self.member.notes, self.other.name):
            self.assertNotContains(home, text)

    def test_wrong_password_does_not_authenticate(self):
        response = self.login(password='FitStudio-Test-Only-Wrong-Login!')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotContains(response, 'FitStudio-Test-Only-Wrong-Login!')

    def test_disabled_user_cannot_login(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.login().status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unbound_user_cannot_login_or_open_home(self):
        self.assertEqual(self.login(username=self.unbound.username).status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.client.force_login(self.unbound)
        self.assertEqual(self.client.get('/portal/').status_code, 403)

    def test_inactive_member_cannot_login_and_existing_session_loses_access(self):
        self.login()
        Member.objects.filter(pk=self.member.pk).update(status=Member.Status.INACTIVE)
        self.assertEqual(self.client.get('/portal/').status_code, 403)
        self.client.logout()
        self.assertEqual(self.login().status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_unbinding_revokes_existing_session(self):
        self.login()
        self.binding.delete()
        self.assertEqual(self.client.get('/portal/').status_code, 403)

    def test_disabled_user_session_requires_login_again(self):
        self.login()
        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertRedirects(self.client.get('/portal/'), '/portal/login/?next=/portal/')

    def test_staff_and_superuser_denied_even_with_a_binding(self):
        from studio.models import MemberAccount
        for actor in (self.staff, self.admin):
            with self.subTest(actor=actor.username):
                # Simulate a legacy or programmatic misconfiguration, bypassing model clean.
                MemberAccount.objects.create(user=actor, member=Member.objects.create(name='虚构误绑定'))
                self.client.logout()
                self.assertEqual(self.login(username=actor.username).status_code, 200)
                self.assertNotIn('_auth_user_id', self.client.session)
                self.client.force_login(actor)
                self.assertEqual(self.client.get('/portal/').status_code, 403)

    def test_promoting_member_to_staff_revokes_portal_access(self):
        self.login()
        get_user_model().objects.filter(pk=self.user.pk).update(is_staff=True)
        self.assertEqual(self.client.get('/portal/').status_code, 403)

    def test_identity_cannot_be_selected_by_query_post_or_url(self):
        self.client.force_login(self.user)
        home = self.client.get('/portal/', {'member_id': self.other.pk, 'user_id': self.other_user.pk})
        self.assertContains(home, self.member.name)
        self.assertNotContains(home, self.other.name)
        self.assertEqual(self.client.post('/portal/', {'member_id': self.other.pk}).status_code, 405)
        for path in (f'/portal/{self.other.pk}/', f'/portal/members/{self.other.pk}/'):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_member_denied_staff_functions_even_if_given_model_permissions(self):
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label='studio'))
        self.client.force_login(self.user)
        for path in ('/', '/members/', '/members/add/', f'/members/{self.other.pk}/edit/',
                     f'/members/{self.other.pk}/lessons/', '/courses/', '/bookings/add/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/admin/login/'))

    def test_original_staff_login_and_pages_still_work(self):
        response = self.client.post('/admin/login/?next=/members/', {
            'username': self.staff.username, 'password': self.password, 'next': '/members/'})
        self.assertRedirects(response, '/members/')
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/members/').status_code, 200)
        self.assertEqual(self.client.get('/members/add/').status_code, 403)

    def test_anonymous_redirect_and_named_routes(self):
        self.assertEqual(reverse('studio:portal_home'), '/portal/')
        self.assertEqual(reverse('studio:portal_login'), '/portal/login/')
        self.assertRedirects(self.client.get('/portal/'), '/portal/login/?next=/portal/')

    def test_no_public_registration(self):
        for path in ('/register/', '/signup/', '/portal/register/', '/portal/signup/',
                     '/accounts/register/', '/portal/bookings/add/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_client_member_id_and_external_next_ignored_during_login(self):
        response = self.login(member_id=self.other.pk, next='https://example.invalid/')
        self.assertRedirects(response, '/portal/')
        self.assertContains(self.client.get('/portal/'), self.member.name)

    def test_login_and_logout_require_csrf_and_logout_requires_post(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/portal/login/', {
            'username': self.user.username, 'password': self.password}).status_code, 403)
        client.get('/portal/login/')
        response = client.post('/portal/login/', {
            'username': self.user.username, 'password': self.password,
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/portal/logout/').status_code, 405)
        self.assertEqual(client.post('/portal/logout/').status_code, 403)
        response = client.post('/portal/logout/', {
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertRedirects(response, '/portal/login/')
        self.assertNotIn('_auth_user_id', client.session)

    def test_identity_escaping_cache_headers_and_mobile_login(self):
        self.member.name = '<script>fictional</script>'
        self.member.save()
        self.client.force_login(self.user)
        response = self.client.get('/portal/')
        self.assertContains(response, '&lt;script&gt;fictional&lt;/script&gt;')
        self.assertNotContains(response, '<script>fictional</script>')
        self.assertIn('no-store', response['Cache-Control'])
        self.client.logout()
        response = self.client.get('/portal/login/')
        for fragment in ('FitStudio', 'name="viewport"', 'autocomplete="username"',
                         'autocomplete="current-password"', 'csrfmiddlewaretoken', 'portal.css'):
            self.assertContains(response, fragment)

    def test_binding_uniqueness_and_protected_member_user(self):
        from studio.models import MemberAccount
        for fields in (dict(user=self.user, member=self.other), dict(user=self.other_user, member=self.member)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                MemberAccount.objects.create(**fields)
        with self.assertRaises(ProtectedError):
            self.member.delete()
        with self.assertRaises(ProtectedError):
            self.user.delete()

    def test_admin_can_create_binding_and_rejects_staff_binding(self):
        from studio.models import MemberAccount
        self.client.force_login(self.admin)
        member = Member.objects.create(name='虚构待开通会员')
        response = self.client.post('/admin/studio/memberaccount/add/', {
            'user': self.unbound.pk, 'member': member.pk, '_save': '保存'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(MemberAccount.objects.filter(user=self.unbound, member=member).exists())
        with self.assertRaises(ValidationError):
            MemberAccount(user=self.staff, member=Member.objects.create(name='虚构职员绑定')).full_clean()
        denied = self.client.post('/admin/studio/memberaccount/add/', {
            'user': self.staff.pk, 'member': self.other.pk, '_save': '保存'})
        self.assertEqual(denied.status_code, 200)
        self.assertFalse(MemberAccount.objects.filter(user=self.staff).exists())
