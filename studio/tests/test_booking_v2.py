import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from django.test import TestCase, override_settings, Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from studio.models import Booking, MemberAccount, MemberRuleConfirmation, LessonEntry
from studio.services import BusinessError, credit_balance, available_credits, create_booking
from .test_lessons import LessonFixtures

@override_settings(FITSTUDIO_BOOKING_MAX_DAYS=3)
class DirectBookingTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp();self.purchase()
        self.course.name='私教课';self.course.save()
        self.user=get_user_model().objects.create_user('自主预约会员')
        MemberAccount.objects.create(user=self.user,member=self.member)
        self.config=override_settings(FITSTUDIO_BOOKING_COACH_ID=self.coach.pk,FITSTUDIO_BOOKING_COURSE_TYPE_ID=self.course.pk)
        self.config.enable();self.addCleanup(self.config.disable)
        self.now=timezone.make_aware(datetime(2026,11,9,9,0))
        self.clock=patch('django.utils.timezone.now',return_value=self.now);self.clock.start();self.addCleanup(self.clock.stop)
        self.start=self.now.replace(hour=10)
    def book(self,**kw):
        from studio.booking_v2 import book_direct
        args=dict(actor=self.user,starts_at=self.start,request_id=uuid.uuid4());args.update(kw)
        return book_direct(**args)
    def test_without_slot_and_idempotency(self):
        key=uuid.uuid4();a=self.book(request_id=key);b=self.book(request_id=key)
        self.assertEqual(a.pk,b.pk);self.assertIsNone(a.slot_id)
        self.assertEqual(a.ends_at-a.starts_at,timedelta(minutes=60))
        self.assertEqual((credit_balance(self.member.pk,self.course.pk),available_credits(self.member.pk,self.course.pk)),(10,9))
        with self.assertRaises(BusinessError):self.book(request_id=key,starts_at=self.start+timedelta(hours=2))
    def test_business_boundaries(self):
        for dt in [self.start.replace(hour=9,minute=30),self.start.replace(hour=21,minute=30),self.start.replace(hour=10,minute=15),self.start+timedelta(days=4),self.start-timedelta(days=1),self.start.replace(second=1)]:
            with self.subTest(dt=dt),self.assertRaises(BusinessError):self.book(starts_at=dt)
        last=self.book(starts_at=self.start.replace(hour=21));self.assertEqual(timezone.localtime(last.ends_at).hour,22)
    def test_sunday(self):
        with patch('django.utils.timezone.now',return_value=self.now+timedelta(days=5)):
            with self.assertRaises(BusinessError):self.book(starts_at=self.start+timedelta(days=6))
    def test_missing_invalid_config(self):
        for value in ['',None,'abc',-1]:
            with override_settings(FITSTUDIO_BOOKING_MAX_DAYS=value),self.assertRaises(BusinessError):self.book()
        with override_settings(FITSTUDIO_BOOKING_COACH_ID=99999),self.assertRaises(BusinessError):self.book()
    def test_conflict_and_adjacent(self):
        self.book()
        with self.assertRaises(BusinessError):self.book(starts_at=self.start+timedelta(minutes=30))
        self.book(starts_at=self.start+timedelta(hours=1))
    def test_coach_booking_conflicts(self):
        create_booking(member_id=self.member.pk,coach_id=self.coach.pk,course_type_id=self.course.pk,starts_at=self.start,duration_minutes=60,actor=self.actor,request_id=uuid.uuid4())
        with self.assertRaises(BusinessError):self.book(starts_at=self.start+timedelta(minutes=30))
    def test_reschedule_keeps_duration_and_lock(self):
        from studio.booking_v2 import reschedule_direct
        from studio.portal_services import member_locked
        from studio.portal_rules import RULES
        a=self.book();key=uuid.uuid4()
        kw=dict(actor=self.user,starts_at=self.start+timedelta(days=1),request_id=key,confirmed=True,rule_version=RULES['reschedule']['version'],expected_starts_at=a.starts_at.isoformat())
        b=reschedule_direct(a.pk,**kw)
        self.assertEqual(b.ends_at-b.starts_at,timedelta(hours=1));self.assertTrue(member_locked(b))
        self.assertEqual(reschedule_direct(a.pk,**kw).pk,b.pk)
        self.assertEqual(MemberRuleConfirmation.objects.count(),1)
        with self.assertRaises(BusinessError):reschedule_direct(b.pk,**dict(kw,request_id=uuid.uuid4(),expected_starts_at=b.starts_at.isoformat()))
    def test_http_confirm_csrf_and_repeat(self):
        self.client.force_login(self.user)
        response=self.client.get('/portal/reserve/',{'day':'2026-11-09'})
        self.assertEqual(response.status_code,200)
        r=self.client.post('/portal/reserve/',{'day':'2026-11-09','start_time':'10:00'})
        self.assertContains(r,'确认预约')
        data={'confirmation_token':r.context['confirmation_token']}
        self.assertEqual(self.client.post('/portal/reserve/submit/',data).status_code,302)
        self.assertEqual(self.client.post('/portal/reserve/submit/',data).status_code,302)
        self.assertEqual(Booking.objects.count(),1)
        c=Client(enforce_csrf_checks=True);c.force_login(self.user)
        self.assertEqual(c.post('/portal/reserve/submit/',data).status_code,403)

    def test_rolls_back_when_audit_fails(self):
        from studio.booking_v2 import reschedule_direct
        from studio.portal_rules import RULES
        a=self.book()
        with patch('studio.booking_v2.MemberRuleConfirmation.objects.create',side_effect=RuntimeError('test failure')):
            with self.assertRaises(RuntimeError):reschedule_direct(a.pk,actor=self.user,starts_at=self.start+timedelta(days=1),request_id=uuid.uuid4(),confirmed=True,rule_version=RULES['reschedule']['version'],expected_starts_at=a.starts_at.isoformat())
        a.refresh_from_db();self.assertEqual(a.status,'pending');self.assertEqual(Booking.objects.count(),1)
    def test_other_member_cannot_move(self):
        from django.core.exceptions import PermissionDenied
        from studio.booking_v2 import reschedule_direct
        from studio.portal_rules import RULES
        a=self.book();other=get_user_model().objects.create_user('other-v2')
        from studio.models import Member
        MemberAccount.objects.create(user=other,member=Member.objects.create(name='其他会员'))
        with self.assertRaises(PermissionDenied):reschedule_direct(a.pk,actor=other,starts_at=self.start+timedelta(days=1),request_id=uuid.uuid4(),confirmed=True,rule_version=RULES['reschedule']['version'],expected_starts_at=a.starts_at.isoformat())
    def test_tamper_and_foreign_confirmation(self):
        self.client.force_login(self.user)
        r=self.client.post('/portal/reserve/',{'day':'2026-11-09','start_time':'10:00'})
        token=r.context['confirmation_token']
        self.client.post('/portal/reserve/submit/',{'confirmation_token':token+'x'})
        self.assertFalse(Booking.objects.exists())
    def test_three_day_window_includes_third_day(self):
        self.book(starts_at=self.start+timedelta(days=3))
    def test_legacy_duration_preserved(self):
        from studio.booking_v2 import reschedule_direct
        from studio.portal_rules import RULES
        a=create_booking(member_id=self.member.pk,coach_id=self.coach.pk,course_type_id=self.course.pk,starts_at=self.start,duration_minutes=90,actor=self.actor,request_id=uuid.uuid4())
        b=reschedule_direct(a.pk,actor=self.user,starts_at=self.start+timedelta(days=1),request_id=uuid.uuid4(),confirmed=True,rule_version=RULES['reschedule']['version'],expected_starts_at=a.starts_at.isoformat())
        self.assertEqual(b.ends_at-b.starts_at,timedelta(minutes=90))

    def test_legacy_slot_cannot_bypass_business_hours(self):
        from studio.models import AvailableSlot
        from studio.portal_services import member_book,slot_stamp
        slot=AvailableSlot.objects.create(coach=self.coach,course_type=self.course,starts_at=self.start.replace(hour=9,minute=30),duration_minutes=60)
        with self.assertRaises(BusinessError):member_book(actor=self.user,slot_id=slot.pk,expected_slot=slot_stamp(slot),request_id=uuid.uuid4())
    def test_legacy_links_forward_to_direct_ui(self):
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get('/portal/courses/'),'/portal/reserve/')
        b=self.book()
        self.assertRedirects(self.client.get(f'/portal/bookings/{b.pk}/reschedule/'),f'/portal/bookings/{b.pk}/move/')


from django.test import TransactionTestCase
from django.db import close_old_connections,connections
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from studio.models import Member

@override_settings(FITSTUDIO_BOOKING_MAX_DAYS=3)
class DirectConcurrencyTests(LessonFixtures,TransactionTestCase):
    def setUp(self):
        super().setUp();self.purchase(credits=1)
        self.user=get_user_model().objects.create_user('race-v2-member')
        MemberAccount.objects.create(user=self.user,member=self.member)
        self.other=Member.objects.create(name='另一位并发会员')
        self.purchase(member_id=self.other.pk,credits=1)
        self.other_user=get_user_model().objects.create_user('race-v2-other')
        MemberAccount.objects.create(user=self.other_user,member=self.other)
        self.config=override_settings(FITSTUDIO_BOOKING_COACH_ID=self.coach.pk,FITSTUDIO_BOOKING_COURSE_TYPE_ID=self.course.pk)
        self.config.enable();self.addCleanup(self.config.disable)
        self.now=timezone.make_aware(datetime(2026,11,9,9));self.start=self.now.replace(hour=10)
        self.clock=patch('django.utils.timezone.now',return_value=self.now);self.clock.start();self.addCleanup(self.clock.stop)
    def race(self,first,second):
        gate=Barrier(2)
        def run(fn):
            close_old_connections()
            try:
                gate.wait(timeout=5)
                try:fn();return 'ok'
                except BusinessError:return 'rejected'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks=[pool.submit(run,fn) for fn in [first,second]]
            return [t.result(timeout=20) for t in tasks]
    def direct(self,user,start,key=None):
        from studio.booking_v2 import book_direct
        return book_direct(actor=user,starts_at=start,request_id=key or uuid.uuid4())
    def test_two_members_overlapping_different_starts(self):
        result=self.race(lambda:self.direct(self.user,self.start),lambda:self.direct(self.other_user,self.start+timedelta(minutes=30)))
        self.assertEqual(sorted(result),['ok','rejected']);self.assertEqual(Booking.objects.count(),1)
    def test_member_and_coach_race(self):
        def coach():create_booking(member_id=self.other.pk,coach_id=self.coach.pk,course_type_id=self.course.pk,starts_at=self.start,duration_minutes=60,actor=self.actor,request_id=uuid.uuid4())
        self.assertEqual(sorted(self.race(lambda:self.direct(self.user,self.start),coach)),['ok','rejected'])
        self.assertEqual(Booking.objects.count(),1)
    def test_one_credit_two_nonoverlapping_bookings(self):
        self.assertEqual(sorted(self.race(lambda:self.direct(self.user,self.start),lambda:self.direct(self.user,self.start+timedelta(hours=2)))),['ok','rejected'])
        self.assertEqual(available_credits(self.member.pk,self.course.pk),0)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),1)
    def test_parallel_retry_same_id(self):
        key=uuid.uuid4()
        self.assertEqual(self.race(lambda:self.direct(self.user,self.start,key),lambda:self.direct(self.user,self.start,key)),['ok','ok'])
        self.assertEqual(Booking.objects.count(),1)
