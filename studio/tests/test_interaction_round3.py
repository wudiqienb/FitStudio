import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.utils import timezone
from studio.models import AvailableSlot, Booking, Member, MemberAccount, LessonEntry
from studio.portal_services import member_book, slot_stamp
from studio.services import credit_snapshot
from .test_lessons import LessonFixtures

class MemberInteractionTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp(); self.purchase(credits=3)
        self.user=get_user_model().objects.create_user('interaction_member')
        MemberAccount.objects.create(user=self.user,member=self.member)
        self.client.force_login(self.user)
        self.slot=AvailableSlot.objects.create(course_type=self.course,coach=self.coach,starts_at=timezone.now()+timedelta(days=5),duration_minutes=60)
        self.target=AvailableSlot.objects.create(course_type=self.course,coach=self.coach,starts_at=self.slot.starts_at+timedelta(days=1),duration_minutes=60)

    def book_data(self):
        r=self.client.get(f'/portal/courses/{self.slot.pk}/confirm/')
        return {k:str(r.context[v]) for k,v in [('request_id','request_id'),('expected_slot','stamp'),('confirmation_token','confirmation_token')]}

    def post(self,url,data):
        return self.client.post(url,data,HTTP_X_FITSTUDIO_INTERACTION='1')

    def book(self):
        return member_book(actor=self.user,slot_id=self.slot.pk,request_id=uuid.uuid4(),expected_slot=slot_stamp(self.slot))

    def confirm(self,b,action):
        r=self.client.get(f'/portal/bookings/{b.pk}/{action}/')
        data={k:str(v) for k,v in r.context['form'].initial.items()}; data['confirmed']='on'
        if action=='reschedule':data['slot_selection']=f'{self.target.pk}|{slot_stamp(self.target)}'
        return data

    def test_booking_success_actual_and_available(self):
        r=self.post(f'/portal/courses/{self.slot.pk}/book/',self.book_data());self.assertEqual(r.status_code,200)
        d=r.json();self.assertTrue(d['ok']);self.assertEqual(d['account'],{'balance':3,'available':2,'held':1});self.assertIn('已占用1',d['message']);self.assertIn('待上课',d['html'])
        self.assertEqual(LessonEntry.objects.filter(booking__isnull=False).count(),0)

    def test_booking_failure_does_not_return_success_card(self):
        data=self.book_data();self.slot.active=False;self.slot.save()
        r=self.post(f'/portal/courses/{self.slot.pk}/book/',data);self.assertEqual(r.status_code,422);self.assertFalse(r.json()['ok']);self.assertNotIn('html',r.json());self.assertFalse(Booking.objects.exists())

    def test_booking_duplicate_only_holds_once(self):
        data=self.book_data()
        for _ in range(2):self.assertTrue(self.post(f'/portal/courses/{self.slot.pk}/book/',data).json()['ok'])
        self.assertEqual(Booking.objects.count(),1);self.assertEqual(credit_snapshot(self.member.pk,self.course.pk)['available'],2)

    def test_booking_bad_token(self):
        data=self.book_data();data['confirmation_token']='bad'
        r=self.post(f'/portal/courses/{self.slot.pk}/book/',data);self.assertFalse(r.json()['ok']);self.assertFalse(Booking.objects.exists())

    def test_cancel_success_release(self):
        b=self.book();d=self.post(f'/portal/bookings/{b.pk}/cancel/',self.confirm(b,'cancel')).json()
        self.assertTrue(d['ok']);self.assertIn('不扣课',d['message']);self.assertIn('释放',d['message']);self.assertEqual(d['account'],{'balance':3,'available':3,'held':0});self.assertIn('已取消',d['html'])

    def test_cancel_requires_individual_confirmation(self):
        b=self.book();data=self.confirm(b,'cancel');data.pop('confirmed')
        r=self.post(f'/portal/bookings/{b.pk}/cancel/',data);self.assertEqual(r.status_code,422);self.assertFalse(r.json()['ok']);self.assertTrue(r.json()['reconfirm']);b.refresh_from_db();self.assertEqual(b.status,'pending')

    def test_cancel_outcome_changed_reconfirmation(self):
        b=self.book();data=self.confirm(b,'cancel');data['expected_outcome']='late'
        r=self.post(f'/portal/bookings/{b.pk}/cancel/',data);self.assertEqual(r.status_code,422);self.assertEqual(r.json()['confirmation']['expected_outcome'],'normal');self.assertIn('不扣课',r.json()['outcome_text']);self.assertIn('课程当天',r.json()['rule_text']);self.assertIn(self.course.name,r.json()['booking_summary'])

    def test_cancel_duplicate(self):
        b=self.book();data=self.confirm(b,'cancel')
        for _ in range(2):self.assertTrue(self.post(f'/portal/bookings/{b.pk}/cancel/',data).json()['ok'])
        self.assertEqual(credit_snapshot(self.member.pk,self.course.pk)['balance'],3)

    def test_reschedule_success_locks_and_does_not_double_hold(self):
        b=self.book();d=self.post(f'/portal/bookings/{b.pk}/reschedule/',self.confirm(b,'reschedule')).json()
        self.assertTrue(d['ok']);self.assertTrue(d['locked']);self.assertIn('已锁定',d['html']);self.assertNotIn('申请改期',d['html']);self.assertNotIn('>取消预约<',d['html']);self.assertEqual(d['account']['available'],2)

    def test_reschedule_failure_does_not_lock(self):
        b=self.book();data=self.confirm(b,'reschedule');self.target.active=False;self.target.save()
        r=self.post(f'/portal/bookings/{b.pk}/reschedule/',data);self.assertEqual(r.status_code,422);self.assertFalse(r.json()['ok']);self.assertNotIn('locked',r.json());self.assertContains(self.client.get(f'/portal/bookings/{b.pk}/'),'申请改期')

    def test_reschedule_requires_checkbox(self):
        b=self.book();data=self.confirm(b,'reschedule');data.pop('confirmed')
        self.assertEqual(self.post(f'/portal/bookings/{b.pk}/reschedule/',data).status_code,422)

    def test_locked_detail_and_list_have_no_actions(self):
        b=self.book();self.post(f'/portal/bookings/{b.pk}/reschedule/',self.confirm(b,'reschedule'))
        new=Booking.objects.get(previous_booking=b)
        for url in (f'/portal/bookings/{new.pk}/','/portal/bookings/'):
            r=self.client.get(url);self.assertContains(r,'已锁定');self.assertNotContains(r,'申请改期');self.assertNotContains(r,'>取消预约<')
        for action in ('cancel','reschedule'):
            self.assertEqual(self.post(f'/portal/bookings/{new.pk}/{action}/',{}).status_code,422)

    def test_home_three_credit_numbers_and_counts(self):
        self.book();r=self.client.get('/portal/');self.assertContains(r,'实际剩余');self.assertContains(r,'预约占用');self.assertContains(r,'可用课时');self.assertEqual(r.context['accounts'][0]['held'],1);self.assertContains(r,'预约概览')

    def test_empty_state_guidance(self):
        self.assertContains(self.client.get('/portal/'),'去预约课程');self.assertContains(self.client.get('/portal/bookings/'),'去预约课程')

    def test_other_member_cannot_view_or_operate(self):
        b=self.book();u=get_user_model().objects.create_user('other_interaction');m=Member.objects.create(name='另一会员');MemberAccount.objects.create(user=u,member=m);self.client.force_login(u)
        self.assertEqual(self.post(f'/portal/bookings/{b.pk}/cancel/',{}).status_code,404);self.assertNotContains(self.client.get('/portal/bookings/'),self.course.name)

    def test_csrf_preserved(self):
        c=Client(enforce_csrf_checks=True);c.force_login(self.user)
        self.assertEqual(c.post(f'/portal/courses/{self.slot.pk}/book/',self.book_data(),HTTP_X_FITSTUDIO_INTERACTION='1').status_code,403)

    def test_native_forms_preserved(self):
        data=self.book_data();r=self.client.post(f'/portal/courses/{self.slot.pk}/book/',data);self.assertEqual(r.status_code,302)

    def test_feedback_is_not_queued_twice(self):
        self.post(f'/portal/courses/{self.slot.pk}/book/',self.book_data());self.assertNotContains(self.client.get('/portal/'),'data-fi-message')

    def test_pagination_preserved(self):
        for i in range(21):
            Booking.objects.create(member=self.member,coach=self.coach,course_type=self.course,course_name='历史测试',starts_at=self.slot.starts_at+timedelta(days=20+i),ends_at=self.slot.starts_at+timedelta(days=20+i,hours=1),status='cancelled',credits_cost=1,created_by=self.actor)
        r=self.client.get('/portal/bookings/');self.assertEqual(len(r.context['bookings']),20);self.assertContains(r,'下一页');self.assertEqual(len(self.client.get('/portal/bookings/?page=2').context['bookings']),1)

    def test_late_cancel_uses_real_ledger(self):
        from unittest.mock import patch
        b=self.book()
        with patch('studio.portal_services.timezone.now',return_value=self.slot.starts_at-timedelta(hours=1)):
            data=self.confirm(b,'cancel');d=self.post(f'/portal/bookings/{b.pk}/cancel/',data).json()
        self.assertTrue(d['ok']);self.assertIn('已扣除1',d['message']);self.assertEqual(d['account'],{'balance':2,'available':2,'held':0})
        self.assertEqual(LessonEntry.objects.filter(booking=b,kind='late_cancel').count(),1)

    def test_failed_reschedule_can_retry_without_lock(self):
        b=self.book();data=self.confirm(b,'reschedule');self.target.active=False;self.target.save()
        self.assertFalse(self.post(f'/portal/bookings/{b.pk}/reschedule/',data).json()['ok'])
        self.target.active=True;self.target.save()
        self.assertTrue(self.post(f'/portal/bookings/{b.pk}/reschedule/',data).json()['ok'])
