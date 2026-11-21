import uuid
from decimal import Decimal
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.models import LogEntry
from django.core.exceptions import PermissionDenied
from django.db import connections, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings, Client
from django.urls import reverse

from studio.models import Member, MemberAccount, CourseType, Package, Purchase, LessonEntry
from studio.services import BusinessError, credit_balance, register_purchase
from studio.quick_services import quick_open, quick_renew, reset_member_credentials


class QuickFixtures:
    def setUp(self):
        self.actor = get_user_model().objects.create_user('quick_coach', password='FitStudio-Test-Only-Quick-Coach!', is_staff=True)
        self.actor.user_permissions.add(*Permission.objects.filter(content_type__app_label='studio', codename__in=['quick_open_member','quick_renew_member']))
        self.course = CourseType.objects.create(name='本地私教')
        self.config = override_settings(FITSTUDIO_QUICK_COURSE_TYPE_ID=self.course.pk)
        self.config.enable(); self.addCleanup(self.config.disable)
        self.member = Member.objects.create(name='老会员', phone='00002001234')
        self.client.force_login(self.actor)

    def open(self, **kw):
        data = dict(actor=self.actor, request_id=uuid.uuid4(), name='新会员', phone='00001001234', credits=12, amount=Decimal('1999.90'))
        data.update(kw); return quick_open(**data)

    def renew(self, **kw):
        data = dict(actor=self.actor, request_id=uuid.uuid4(), member_id=self.member.pk, credits=5, amount=Decimal('888.88'))
        data.update(kw); return quick_renew(**data)


class QuickTests(QuickFixtures, TestCase):
    def test_same_origin_referrer_and_real_csrf_submission(self):
        c=Client(enforce_csrf_checks=True); c.force_login(self.actor)
        r=c.get('/quick/open/')
        self.assertContains(r, '<meta name="referrer" content="same-origin">', html=True)
        token=c.cookies['csrftoken'].value
        data=dict(name='CSRF验收',phone='00000509996',credits=2,amount='10.01',
                  request_id=str(uuid.uuid4()),csrfmiddlewaretoken=token)
        self.assertEqual(c.post('/quick/open/',data,HTTP_ORIGIN='http://testserver').status_code,200)
        self.assertEqual(Purchase.objects.count(),1)
        self.assertEqual(c.post('/quick/open/',data,HTTP_ORIGIN='http://evil.invalid').status_code,403)

    def test_open_full_flow_and_decimal(self):
        order, password = self.open()
        p = order.purchase
        self.assertEqual((p.package_name,p.credits,p.amount,p.course_type_id),('私教课',12,Decimal('1999.90'),self.course.pk))
        self.assertEqual(credit_balance(p.member_id,self.course.pk),12)
        e = LessonEntry.objects.get(purchase=p)
        self.assertEqual((e.delta,e.balance_before,e.balance_after),(12,0,12))
        user = MemberAccount.objects.get(member=p.member).user
        self.assertTrue(user.check_password(password)); self.assertEqual(len(password),10)
        self.assertFalse(set(password) & set('O0Il'))
        from django.contrib.auth.password_validation import validate_password
        validate_password(password,user)
        self.assertEqual(user.username,'新会员')
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertFalse(user.groups.exists() or user.user_permissions.exists())
        self.assertNotEqual(user.username,p.member.phone)
        self.assertFalse(LogEntry.objects.filter(change_message__contains=password).exists())
        portal=Client(); self.assertTrue(portal.login(username=user.username,password=password))
        self.assertRedirects(portal.get('/portal/'),'/portal/change-password/')
        self.assertContains(portal.get('/portal/change-password/'),'修改')

    def test_renew_preserves_account_password_and_profile(self):
        user=get_user_model().objects.create_user('existing',password='FitStudio-Test-Only-Renewal!')
        MemberAccount.objects.create(member=self.member,user=user)
        original=user.password
        o=self.renew(); self.renew(credits=2,amount=Decimal('0.01'))
        user.refresh_from_db()
        self.assertEqual(user.password,original); self.assertEqual(Member.objects.count(),1)
        self.assertEqual(MemberAccount.objects.count(),1)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),7)
        self.assertEqual(o.purchase.amount,Decimal('888.88'))
        self.assertEqual(Package.objects.count(),1)

    def test_renew_without_account_does_not_open_one(self):
        self.renew(); self.assertEqual(MemberAccount.objects.count(),0)

    def test_package_reuse_ignores_quantity_price_and_historical_duplicates(self):
        for _ in range(2): Package.objects.create(name='私教课',course_type=self.course,credits=10,price=100)
        old=list(Package.objects.values_list('pk','credits','price'))
        a=self.renew(); b=self.open()[0]
        self.assertEqual(a.purchase.package_id,b.purchase.package_id)
        self.assertLessEqual(Package.objects.count(),3)
        for pk,credits,price in old:
            p=Package.objects.get(pk=pk); self.assertEqual((p.credits,p.price),(credits,price))

    def test_duplicate_open_and_renew(self):
        key=uuid.uuid4(); first,password=self.open(request_id=key); again,secret=self.open(request_id=key)
        self.assertEqual(first.pk,again.pk); self.assertIsNone(secret)
        key=uuid.uuid4(); a=self.renew(request_id=key); b=self.renew(request_id=key)
        self.assertEqual(a.pk,b.pk)
        self.assertEqual(Purchase.objects.count(),2); self.assertEqual(LessonEntry.objects.count(),2)
        self.assertEqual(MemberAccount.objects.count(),1)
        with self.assertRaises(BusinessError): self.renew(request_id=key,amount=Decimal('1'))
        with self.assertRaises(BusinessError): self.open(request_id=key)

    def test_conflict_including_formatted_historical_phone(self):
        self.member.phone='+86 000-0100-1234'; self.member.save()
        with self.assertRaises(BusinessError): self.open()
        self.assertEqual(Member.objects.count(),1); self.assertEqual(Purchase.objects.count(),0)

    def test_inactive_never_activated(self):
        self.member.status='inactive'; self.member.save()
        with self.assertRaises(BusinessError): self.renew()
        self.member.refresh_from_db(); self.assertEqual(self.member.status,'inactive')

    def test_invalid_configuration_rejected(self):
        for value in [None,'',0,'nonsense',999999]:
            with self.subTest(value=value),override_settings(FITSTUDIO_QUICK_COURSE_TYPE_ID=value):
                with self.assertRaises(BusinessError): self.renew()
        self.course.active=False; self.course.save()
        with self.assertRaises(BusinessError): self.open()
        self.assertEqual(Purchase.objects.count(),0)

    def test_invalid_money_and_credits(self):
        for value in [Decimal('-1'),Decimal('1.001'),Decimal('NaN'),Decimal('Infinity'),None]:
            with self.subTest(amount=value),self.assertRaises(BusinessError): self.renew(amount=value)
        for value in [0,-1,1.5,True,'5']:
            with self.subTest(credits=value),self.assertRaises(BusinessError): self.open(credits=value)
        self.assertEqual(Purchase.objects.count(),0)

    def test_rollback_at_ledger_account_and_audit(self):
        for target in ['studio.services.LessonEntry.objects.create','studio.quick_services.MemberAccount.objects.create','studio.quick_services.LogEntry.objects.log_actions']:
            with self.subTest(target=target),patch(target,side_effect=RuntimeError('simulated')):
                with self.assertRaises(RuntimeError): self.open()
            self.assertEqual(Member.objects.count(),1); self.assertEqual(Purchase.objects.count(),0)
            self.assertEqual(Package.objects.count(),0); self.assertEqual(MemberAccount.objects.count(),0)
        with patch('studio.services.LessonEntry.objects.create',side_effect=RuntimeError('simulated')):
            with self.assertRaises(RuntimeError): self.renew()
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),0)

    def test_function_permissions_and_service_boundary(self):
        with self.assertRaises(PermissionDenied):
            register_purchase(actor=self.actor,member_id=self.member.pk,package_id=1,purchased_on=None,request_id=uuid.uuid4(),reason='购课')
        self.actor.user_permissions.clear(); self.actor=get_user_model().objects.get(pk=self.actor.pk)
        with self.assertRaises(PermissionDenied): self.open()
        with self.assertRaises(PermissionDenied): self.renew()

    def test_pages_search_selection_and_receipt(self):
        self.assertEqual(self.client.get('/quick/open/').status_code,200)
        response=self.client.get('/quick/renew/',{'q':'0000200'})
        self.assertContains(response,'老会员'); self.assertNotContains(response,'流水')
        response=self.client.get('/quick/renew/',{'member':self.member.pk})
        self.assertContains(response,'00002001234'); self.assertContains(response,'当前课时余额'); self.assertNotContains(response,'None')
        data=dict(name='页面会员',phone='00000701234',credits=8,amount='1234.56',request_id=str(uuid.uuid4()))
        r=self.client.post('/quick/open/',data); self.assertContains(r,'仅本次显示')
        self.assertIn('no-store',r['Cache-Control'])
        r=self.client.post('/quick/open/',data); self.assertNotContains(r,'仅本次显示')
        data=dict(member=self.member.pk,credits=3,amount='99.99',request_id=str(uuid.uuid4()))
        self.assertEqual(self.client.post('/quick/renew/',data).status_code,200)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),3)

    def test_all_legacy_management_routes_denied(self):
        paths=['/members/','/members/add/',f'/members/{self.member.pk}/edit/',f'/members/{self.member.pk}/delete/',f'/members/{self.member.pk}/account/add/',f'/members/{self.member.pk}/lessons/','/purchases/add/','/catalog/','/catalog/courses/add/','/catalog/packages/add/','/adjustments/add/','/bookings/add/','/courses/backfill/','/admin/auth/user/','/admin/auth/group/','/admin/studio/member/','/admin/studio/memberaccount/','/admin/studio/package/','/admin/studio/purchase/','/admin/studio/lessonentry/']
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code,403)
                self.assertEqual(self.client.post(path,{}).status_code,403)

    def test_independent_account_opening_and_admin_require_superuser(self):
        self.actor.user_permissions.add(*Permission.objects.filter(codename__in=['view_member','add_memberaccount','change_memberaccount','view_memberaccount','add_package','view_package']))
        for path in [f'/members/{self.member.pk}/account/add/','/admin/studio/memberaccount/','/admin/studio/package/']:
            self.assertEqual(self.client.get(path).status_code,403)

    def test_reset_only_admin_and_never_rebinds(self):
        o,old=self.open(); m=o.purchase.member; user=m.login_account.user
        with self.assertRaises(PermissionDenied): reset_member_credentials(actor=self.actor,member_id=m.pk)
        admin=get_user_model().objects.create_superuser('reset_admin',password=None)
        binding,new=reset_member_credentials(actor=admin,member_id=m.pk)
        user.refresh_from_db(); self.assertTrue(user.check_password(new)); self.assertFalse(user.check_password(old))
        self.assertEqual(binding.user_id,user.pk); self.assertEqual(MemberAccount.objects.count(),1)
        self.assertFalse(LogEntry.objects.filter(change_message__contains=new).exists())

    def test_csrf_and_nonstaff(self):
        c=Client(enforce_csrf_checks=True); c.force_login(self.actor)
        self.assertEqual(c.post('/quick/open/',{}).status_code,403)
        self.actor.is_staff=False; self.actor.save()
        self.assertEqual(self.client.get('/quick/open/').status_code,403)

    def test_bad_selection_and_disabled_config_are_readable_errors(self):
        for raw in ['bad','999999999999999999999999999','999999']:
            self.assertContains(self.client.get('/quick/renew/',{'member':raw}),'请选择已有会员')
        with override_settings(FITSTUDIO_QUICK_COURSE_TYPE_ID=''):
            self.assertContains(self.client.get('/quick/renew/',{'member':self.member.pk}),'未配置有效')

    def test_missing_amount_and_extra_fields_cannot_change_behavior(self):
        data=dict(name='表单会员',phone='00000601234',credits=7,request_id=str(uuid.uuid4()),is_superuser='on')
        self.assertEqual(self.client.post('/quick/open/',data).status_code,200)
        self.assertEqual(Purchase.objects.count(),0)
        data['amount']='10.01'; self.client.post('/quick/open/',data)
        user=MemberAccount.objects.get().user; self.assertFalse(user.is_superuser or user.is_staff)

    def test_single_function_permission_is_not_interchangeable(self):
        self.actor.user_permissions.set(Permission.objects.filter(codename='quick_renew_member'))
        self.assertEqual(self.client.get('/quick/open/').status_code,403)
        self.assertEqual(self.client.get('/quick/renew/').status_code,200)
        self.actor.user_permissions.set(Permission.objects.filter(codename='quick_open_member'))
        self.assertEqual(self.client.get('/quick/open/').status_code,200)
        self.assertEqual(self.client.get('/quick/renew/').status_code,403)

    def test_completed_order_cannot_authorize_another_purchase(self):
        from django.db import transaction
        o=self.renew()
        with transaction.atomic(),self.assertRaises(PermissionDenied):
            register_purchase(actor=self.actor,member_id=self.member.pk,package_id=o.package_id,
                credits=99,amount=Decimal('1'),request_id=uuid.uuid4(),purchased_on=o.purchased_on,
                reason='老会员续课',quick_order_id=o.pk)

    def test_reset_page_post_confirmation_and_no_cache(self):
        o,password=self.open(); path=f'/members/{o.member_id}/credentials/reset/'
        self.assertEqual(self.client.get(path).status_code,403)
        admin=get_user_model().objects.create_superuser('page_admin',password=None)
        self.client.force_login(admin)
        self.assertContains(self.client.get(path),'核对会员身份')
        r=self.client.post(path,{'confirm':'yes'}); self.assertContains(r,'仅本次显示')
        self.assertIn('no-store',r['Cache-Control'])


class QuickConcurrencyTests(QuickFixtures, TransactionTestCase):
    def race(self,mode,same_key=False):
        gate=Barrier(2); key=uuid.uuid4(); uid=self.actor.pk
        def work(i):
            close_old_connections()
            try:
                actor=get_user_model().objects.get(pk=uid); gate.wait(timeout=10)
                kw=dict(actor=actor,request_id=key if same_key else uuid.uuid4())
                try:
                    result=self.open(**kw)[0] if mode=='open' else self.renew(**kw)
                    return result.pk
                except BusinessError: return None
            finally: connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool: return list(pool.map(work,range(2)))

    def test_concurrent_identity_conflict(self):
        result=self.race('open'); self.assertEqual(sum(x is not None for x in result),1)
        self.assertEqual(MemberAccount.objects.count(),1); self.assertEqual(Purchase.objects.count(),1)

    def test_concurrent_duplicate_open(self):
        result=self.race('open',True); self.assertEqual(result[0],result[1])
        self.assertEqual(Purchase.objects.count(),1); self.assertEqual(MemberAccount.objects.count(),1)

    def test_concurrent_renew_template_and_balance(self):
        self.race('renew'); self.assertEqual(Purchase.objects.count(),2); self.assertEqual(Package.objects.count(),1)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),10)
        self.assertEqual(list(LessonEntry.objects.order_by('pk').values_list('balance_before','balance_after')),[(0,5),(5,10)])

    def test_concurrent_duplicate_renew(self):
        result=self.race('renew',True); self.assertEqual(result[0],result[1]); self.assertEqual(Purchase.objects.count(),1)


class QuickMigrationTests(TransactionTestCase):
    def test_upgrade_preserves_old_rows_and_duplicate_packages_and_phones(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor=MigrationExecutor(connection)
        leaves=executor.loader.graph.leaf_nodes()
        old=[('studio','0006_memberruleconfirmation_availableslot_booking_slot_and_more')]
        executor.migrate(old)
        try:
            apps=executor.loader.project_state(old).apps
            course=apps.get_model('studio','CourseType').objects.create(name='旧课程')
            for _ in range(2):
                apps.get_model('studio','Member').objects.create(name='同名旧会员',phone='00001001234')
                apps.get_model('studio','Package').objects.create(name='私教课',course_type_id=course.pk,credits=20,price='2000.00')
            models=list(apps.get_app_config('studio').get_models())
            def snapshot(): return {m._meta.db_table:list(m.objects.order_by('pk').values()) for m in models}
            before=snapshot(); MigrationExecutor(connection).migrate(leaves)
            self.assertEqual(before,snapshot())
            with connection.cursor() as cursor:
                cursor.execute('pragma integrity_check'); self.assertEqual(cursor.fetchone()[0],'ok')
                cursor.execute('pragma foreign_key_check'); self.assertEqual(cursor.fetchall(),[])
        finally:
            MigrationExecutor(connection).migrate(leaves)
