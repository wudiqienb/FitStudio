from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission,Group
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
import uuid
from studio.models import Member,Coach,CourseType,Package,Booking,LessonEntry
from studio.services import register_purchase,create_booking
from studio.coach_roles import grant_daily_coach,DAILY_COACH_PERMISSIONS


class DailyCoachWorkbenchTests(TestCase):
    def setUp(self):
        self.coach=get_user_model().objects.create_user('daily_coach',is_staff=True)
        grant_daily_coach(self.coach)
        self.admin=get_user_model().objects.create_superuser('daily_admin',password=None)
        self.member=Member.objects.create(name='日常会员',phone='00008881111')
        self.teacher=Coach.objects.create(name='业务教练')
        self.course=CourseType.objects.create(name='日常课程')
        self.package=Package.objects.create(name='旧套餐',course_type=self.course,credits=20,price=100)
        register_purchase(actor=self.admin,member_id=self.member.pk,package_id=self.package.pk,request_id=uuid.uuid4(),purchased_on=timezone.localdate(),reason='测试购课')
        self.booking=create_booking(actor=self.admin,member_id=self.member.pk,coach_id=self.teacher.pk,course_type_id=self.course.pk,starts_at=timezone.now()+timedelta(hours=2),duration_minutes=60,request_id=uuid.uuid4())
        self.client.force_login(self.coach)

    def test_original_home_and_navigation_and_quick_entries(self):
        r=self.client.get('/')
        self.assertTemplateUsed(r,'studio/home.html');self.assertTemplateUsed(r,'studio/base.html')
        for text in ['今日课程','待处理预约','会员','课程','更多','老会员续课','新增会员']:
            self.assertContains(r,text)
        self.assertNotContains(r,'请选择快速新增会员或老会员续课')
        self.assertContains(r,self.member.name)

    def test_quick_only_permissions_do_not_replace_home(self):
        self.coach.groups.clear()
        self.coach.user_permissions.add(Permission.objects.get(codename='quick_open_member'))
        self.assertTemplateUsed(self.client.get('/'),'studio/home.html')

    def test_daily_business_get_routes(self):
        for url in ['/members/','/members/add/',f'/members/{self.member.pk}/edit/','/courses/','/bookings/add/',f'/bookings/{self.booking.pk}/',f'/bookings/{self.booking.pk}/reschedule/',f'/bookings/{self.booking.pk}/cancel/',f'/bookings/{self.booking.pk}/complete/',f'/bookings/{self.booking.pk}/no-show/',f'/bookings/{self.booking.pk}/coach-cancel/','/admin/studio/coach/','/purchases/add/']:
            with self.subTest(url=url):self.assertEqual(self.client.get(url).status_code,200)

    def test_balance_without_full_ledger_permission(self):
        balance_only=get_user_model().objects.create_user('balance_only',is_staff=True)
        balance_only.user_permissions.add(Permission.objects.get(codename='view_member'))
        self.client.force_login(balance_only)
        self.assertFalse(balance_only.has_perm('studio.view_lessonentry'))
        r=self.client.get(f'/members/{self.member.pk}/balance/')
        self.assertContains(r,'20');self.assertContains(r,'19')
        self.assertNotContains(r,'流水明细')
        self.assertEqual(self.client.get(f'/members/{self.member.pk}/lessons/').status_code,403)
        self.assertContains(self.client.get('/members/'),f'/members/{self.member.pk}/balance/')

    def test_no_sensitive_permissions_or_template_management(self):
        for codename in ['archive_member','restore_booking','late_cancel_booking','view_lessonentry','view_purchase']:
            self.assertTrue(self.coach.has_perm('studio.'+codename),codename)
        forbidden=['add_lessonadjustment','backfill_booking','add_memberaccount','reset_member_credentials','add_package','change_package']
        for codename in forbidden:self.assertFalse(self.coach.has_perm('studio.'+codename),codename)
        self.assertFalse(self.coach.is_superuser)
        for url in ['/admin/auth/user/','/admin/auth/group/','/admin/studio/memberaccount/','/catalog/packages/add/',f'/members/{self.member.pk}/account/add/']:
            self.assertEqual(self.client.get(url).status_code,403)

    def test_incremental_grant_preserves_direct_permissions_and_other_groups(self):
        extra=Group.objects.create(name='已有独立授权组');self.coach.groups.add(extra)
        old=Permission.objects.get(codename='restore_booking');self.coach.user_permissions.add(old)
        grant_daily_coach(self.coach);grant_daily_coach(self.coach)
        self.assertTrue(self.coach.user_permissions.filter(pk=old.pk).exists())
        self.assertTrue(self.coach.groups.filter(pk=extra.pk).exists())
        self.assertEqual(self.coach.groups.filter(name='教练日常业务').count(),1)

    def test_quick_pages_keep_full_workbench_navigation(self):
        for url in ['/quick/open/','/quick/renew/']:
            r=self.client.get(url);self.assertTemplateUsed(r,'studio/base.html');self.assertContains(r,'返回工作台')

    def test_daily_actual_reschedule_cancel_complete_no_show_and_makeup(self):
        from studio.services import reschedule_booking,cancel_booking,complete_booking,mark_no_show,coach_cancel_booking,create_makeup_booking
        user=get_user_model().objects.get(pk=self.coach.pk)
        moved=reschedule_booking(self.booking.pk,actor=user,starts_at=timezone.now()+timedelta(days=4),duration_minutes=60,request_id=uuid.uuid4(),reason='改期')
        cancel_booking(moved.pk,actor=user,reason='正常取消')
        # Historical completion/no-show test uses existing services and the admin to create past bookings.
        for action,hours in [(complete_booking,4),(mark_no_show,7)]:
            booking=create_booking(actor=self.admin,member_id=self.member.pk,coach_id=self.teacher.pk,course_type_id=self.course.pk,starts_at=timezone.now()-timedelta(hours=hours),duration_minutes=60,request_id=uuid.uuid4())
            action(booking.pk,actor=user,reason='日常业务')
        b=create_booking(actor=user,member_id=self.member.pk,coach_id=self.teacher.pk,course_type_id=self.course.pk,starts_at=timezone.now()+timedelta(days=6),duration_minutes=60,request_id=uuid.uuid4())
        coach_cancel_booking(b.pk,actor=user,reason='教练取消')
        create_makeup_booking(b.pk,actor=user,starts_at=timezone.now()+timedelta(days=7),duration_minutes=60,coach_id=self.teacher.pk,request_id=uuid.uuid4(),reason='补课')
        self.assertEqual(LessonEntry.objects.filter(kind__in=['complete','no_show']).count(),2)

    def test_daily_coach_late_cancel_and_restore_are_real_authorized_services(self):
        from unittest.mock import patch
        from studio.services import cancel_booking,restore_booking,credit_balance
        fixed=(timezone.now()+timedelta(days=2)).replace(hour=10,minute=0,second=0,microsecond=0)
        user=get_user_model().objects.get(pk=self.coach.pk)
        with patch('studio.services.timezone.now',return_value=fixed):
            booking=create_booking(actor=user,member_id=self.member.pk,coach_id=self.teacher.pk,course_type_id=self.course.pk,starts_at=fixed+timedelta(hours=1),duration_minutes=60,request_id=uuid.uuid4())
            cancel_booking(booking.pk,actor=user,reason='迟取消核对')
            self.assertEqual(credit_balance(self.member.pk,self.course.pk),19)
            restore_booking(booking.pk,actor=user,reason='误扣核对冲正')
            self.assertEqual(credit_balance(self.member.pk,self.course.pk),20)
