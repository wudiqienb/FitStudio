from unittest.mock import patch
from django.test import TestCase,RequestFactory
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission,Group
from django.core.exceptions import PermissionDenied,ValidationError
from studio.models import Member,MemberArchiveEvent
from studio.forms import MemberForm
from studio.views import MemberUpdateView
from studio.archive_services import archive_member,restore_member
from studio.coach_roles import grant_daily_coach

class RepairBoundaryTests(TestCase):
    def setUp(self):
        self.actor=get_user_model().objects.create_user('boundary_coach',is_staff=True)
        grant_daily_coach(self.actor)
        self.member=Member.objects.create(name='边界会员',phone='00002223333')

    def test_stale_normal_edit_cannot_undo_archive(self):
        form=MemberForm(data=dict(name='旧表单修改',phone=self.member.phone,status='active',joined_on=self.member.joined_on,notes=''),instance=self.member)
        self.assertTrue(form.is_valid())
        archive_member(actor=self.actor,member_id=self.member.pk)
        view=MemberUpdateView();view.object=self.member
        view.request=RequestFactory().post('/');view.request.user=self.actor
        with self.assertRaises(PermissionDenied):view.form_valid(form)
        self.member.refresh_from_db();self.assertEqual(self.member.status,'archived')
        self.assertEqual(MemberArchiveEvent.objects.filter(member=self.member).count(),1)

    def test_permission_revocation_is_rechecked_in_archive_transaction(self):
        self.assertTrue(self.actor.has_perm('studio.archive_member'))
        self.actor.groups.clear()
        with self.assertRaises(PermissionDenied):archive_member(actor=self.actor,member_id=self.member.pk)
        self.member.refresh_from_db();self.assertEqual(self.member.status,'active')

    def test_existing_daily_group_with_system_privileges_is_not_reused(self):
        group=Group.objects.get(name='教练日常业务');group.permissions.add(Permission.objects.get(codename='add_user'))
        target=get_user_model().objects.create_user('target_coach',is_staff=True)
        with self.assertRaises(ValidationError):grant_daily_coach(target)
        self.assertFalse(target.groups.exists())

    def test_role_command_dry_run_never_grants(self):
        from django.core.management import call_command
        from io import StringIO
        target=get_user_model().objects.create_user('preview_coach',is_staff=True)
        output=StringIO();call_command('configure_daily_coach',user_id=target.pk,stdout=output)
        self.assertIn('DRY_RUN',output.getvalue());self.assertFalse(target.groups.exists())

    def test_archived_members_hidden_from_renewal_search(self):
        archive_member(actor=self.actor,member_id=self.member.pk)
        self.client.force_login(self.actor)
        self.assertNotContains(self.client.get('/quick/renew/',{'q':'00002223333'}),'边界会员')

    def test_admin_cannot_change_archived_account_binding(self):
        from studio.models import MemberAccount
        admin=get_user_model().objects.create_superuser('archive_binding_admin',password=None)
        user=get_user_model().objects.create_user('archive_binding_user')
        archive_member(actor=self.actor,member_id=self.member.pk)
        self.client.force_login(admin)
        r=self.client.post('/admin/studio/memberaccount/add/',{'member':self.member.pk,'user':user.pk,'_save':'保存'})
        self.assertEqual(r.status_code,200)
        self.assertContains(r,'归档期间不能新增或更换账号绑定')
        self.assertFalse(MemberAccount.objects.filter(member=self.member).exists())
        restore_member(actor=self.actor,member_id=self.member.pk)
        self.member.refresh_from_db();self.assertEqual(self.member.status,'active')
