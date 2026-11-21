import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import Client, TestCase, TransactionTestCase
from django.utils import timezone

from studio.models import Booking, MemberAccount, Member, LessonEntry
from studio.services import available_credits, credit_balance, create_booking, BusinessError
from studio.portal_services import slot_stamp
from .test_lessons import LessonFixtures


class PortalCoursesEntryTest(TestCase):
    def test_anonymous_courses_redirects_to_member_login(self):
        response = self.client.get('/portal/courses/')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/portal/login/'))


class MemberBookingTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user('fictional_member_phase2')
        MemberAccount.objects.create(user=self.user, member=self.member)
        self.purchase(credits=3)
        self.start = timezone.now().replace(second=0, microsecond=0) + timedelta(days=4)

    def slot(self, start=None, **kwargs):
        from studio.models import AvailableSlot
        return AvailableSlot.objects.create(course_type=self.course, coach=self.coach,
            starts_at=start or self.start, duration_minutes=60, **kwargs)

    def book(self, slot=None, **kwargs):
        from studio.portal_services import member_book
        slot = slot or self.slot()
        return member_book(actor=self.user, slot_id=slot.pk, expected_slot=slot_stamp(slot),
                           request_id=kwargs.get('request_id', uuid.uuid4()))

    def confirmation(self, booking, action='cancel'):
        from studio.portal_rules import RULES
        return {'confirmed': True, 'rule_version': RULES[action]['version'],
                'request_id': uuid.uuid4(), 'expected_starts_at': booking.starts_at.isoformat()}

    def test_member_can_view_published_slots(self):
        self.slot()
        self.client.force_login(self.user)
        response = self.client.get('/portal/courses/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.course.name)
        self.assertContains(response, self.coach.name)

    def test_booking_reserves_without_deduction_and_duplicate_is_idempotent(self):
        slot, key = self.slot(), uuid.uuid4()
        first = self.book(slot, request_id=key)
        second = self.book(slot, request_id=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 3)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 2)
        self.assertFalse(LessonEntry.objects.filter(booking=first).exists())

    def test_changed_slot_must_be_reviewed_before_booking(self):
        from studio.portal_services import member_book
        slot=self.slot()
        shown=slot_stamp(slot)
        slot.starts_at += timedelta(days=1)
        slot.save(update_fields=['starts_at'])
        with self.assertRaises(BusinessError):
            member_book(actor=self.user,slot_id=slot.pk,expected_slot=shown,request_id=uuid.uuid4())
        self.assertFalse(Booking.objects.filter(slot=slot).exists())
        with self.assertRaises(BusinessError):
            member_book(actor=self.user,slot_id=slot.pk,expected_slot='invalid',request_id=uuid.uuid4())

    def test_full_past_inactive_and_multi_credit_slots_rejected(self):
        slot = self.slot()
        self.book(slot)
        with self.assertRaises(BusinessError): self.book(slot)
        with self.assertRaises(BusinessError): self.book(self.slot(start=timezone.now()-timedelta(days=1)))
        with self.assertRaises(BusinessError): self.book(self.slot(start=self.start+timedelta(days=1), active=False))
        self.course.credits_cost = 2
        self.course.save(update_fields=['credits_cost'])
        with self.assertRaises(BusinessError): self.book(self.slot(start=self.start+timedelta(days=2)))

    def test_credit_shortage_and_member_conflict_rejected(self):
        self.book()
        # Existing pending courses reserve all remaining credits.
        self.book(self.slot(start=self.start+timedelta(days=1)))
        self.book(self.slot(start=self.start+timedelta(days=2)))
        with self.assertRaises(BusinessError): self.book(self.slot(start=self.start+timedelta(days=3)))
        other_coach = type(self.coach).objects.create(name='虚构其他教练')
        from studio.models import AvailableSlot
        slot = AvailableSlot.objects.create(course_type=self.course, coach=other_coach, starts_at=self.start, duration_minutes=60)
        with self.assertRaises(BusinessError): self.book(slot)

    def test_member_time_conflict_is_rejected_with_credits_available(self):
        from studio.models import AvailableSlot, Coach
        self.book()
        other = Coach.objects.create(name='虚构第二教练')
        conflicting = AvailableSlot.objects.create(course_type=self.course,coach=other,
            starts_at=self.start+timedelta(minutes=15),duration_minutes=60)
        self.assertEqual(available_credits(self.member.pk,self.course.pk),2)
        with self.assertRaises(BusinessError): self.book(conflicting)

    def test_member_cannot_view_or_change_other_booking(self):
        from studio.portal_services import member_cancel, member_reschedule
        booking = self.book()
        other = get_user_model().objects.create_user('fictional_other_phase2')
        MemberAccount.objects.create(user=other, member=Member.objects.create(name='虚构其他会员'))
        self.client.force_login(other)
        for suffix in ('', 'cancel/', 'reschedule/'):
            self.assertEqual(self.client.get(f'/portal/bookings/{booking.pk}/{suffix}').status_code, 404)
        with self.assertRaises(PermissionDenied):
            member_cancel(booking.pk, actor=other, expected_outcome='normal', **self.confirmation(booking))
        with self.assertRaises(PermissionDenied):
            member_reschedule(booking.pk, actor=other, slot_id=self.slot(start=self.start+timedelta(days=4)).pk, expected_slot='', **self.confirmation(booking, 'reschedule'))

    def test_cancellation_confirmation_and_shanghai_midnight(self):
        from studio.portal_services import member_cancel
        from studio.models import MemberRuleConfirmation
        start = datetime(2027,1,2,18,tzinfo=ZoneInfo('Asia/Shanghai'))
        booking = self.book(self.slot(start=start))
        opts = self.confirmation(booking)
        with patch('django.utils.timezone.now', return_value=datetime(2027,1,1,23,59,59,999999,tzinfo=ZoneInfo('Asia/Shanghai'))):
            with self.assertRaises(BusinessError):
                member_cancel(booking.pk, actor=self.user, expected_outcome='normal', **dict(opts, confirmed=False))
            member_cancel(booking.pk, actor=self.user, expected_outcome='normal', **opts)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),3)
        confirmation = MemberRuleConfirmation.objects.get(booking=booking)
        self.assertEqual((confirmation.member_id,confirmation.user_id,confirmation.action,confirmation.rule_version),
                         (self.member.pk,self.user.pk,'cancel',opts['rule_version']))
        self.assertTrue(confirmation.rule_text)
        self.assertIsNotNone(confirmation.confirmed_at)
        late = self.book(self.slot(start=start+timedelta(days=1)))
        opts2 = self.confirmation(late)
        with patch('django.utils.timezone.now', return_value=datetime(2027,1,3,0,0,tzinfo=ZoneInfo('Asia/Shanghai'))):
            member_cancel(late.pk, actor=self.user, expected_outcome='late', **opts2)
            member_cancel(late.pk, actor=self.user, expected_outcome='late', **opts2)
        self.assertEqual(credit_balance(self.member.pk,self.course.pk),2)
        self.assertEqual(LessonEntry.objects.filter(booking=late,kind='late_cancel').count(),1)

    def test_reschedule_same_day_then_new_booking_is_locked(self):
        from studio.portal_services import member_cancel, member_reschedule
        from studio.models import MemberRuleConfirmation
        booking = self.book()
        target = self.slot(start=self.start+timedelta(days=2))
        local = timezone.localtime(self.start)
        now = local.replace(hour=0,minute=0,second=0,microsecond=0)
        with patch('django.utils.timezone.now',return_value=now):
            opts = self.confirmation(booking,'reschedule')
            with self.assertRaises(BusinessError):
                member_reschedule(booking.pk, actor=self.user, slot_id=target.pk, expected_slot=slot_stamp(target), **dict(opts,confirmed=False))
            new = member_reschedule(booking.pk, actor=self.user, slot_id=target.pk, expected_slot=slot_stamp(target), **opts)
            self.assertEqual(member_reschedule(booking.pk, actor=self.user, slot_id=target.pk, expected_slot=slot_stamp(target), **opts).pk,new.pk)
            self.assertEqual(new.previous_booking_id,booking.pk)
            self.assertEqual(available_credits(self.member.pk,self.course.pk),2)
            self.assertEqual(credit_balance(self.member.pk,self.course.pk),3)
            with self.assertRaisesMessage(BusinessError,'该课程已经改期'):
                member_cancel(new.pk, actor=self.user, expected_outcome='normal', **self.confirmation(new))
            with self.assertRaisesMessage(BusinessError,'该课程已经改期'):
                member_reschedule(new.pk, actor=self.user, slot_id=target.pk, expected_slot=slot_stamp(target), **self.confirmation(new,'reschedule'))
            self.assertEqual(MemberRuleConfirmation.objects.filter(booking=booking,action='reschedule').count(),1)

    def test_failed_reschedule_leaves_original_pending_and_unlocked(self):
        from studio.portal_services import member_cancel, member_reschedule
        booking = self.book()
        target = self.slot(start=timezone.now()-timedelta(days=1))
        with self.assertRaises(BusinessError):
            member_reschedule(booking.pk,actor=self.user,slot_id=target.pk,expected_slot=slot_stamp(target),**self.confirmation(booking,'reschedule'))
        booking.refresh_from_db()
        self.assertEqual(booking.status,'pending')
        member_cancel(booking.pk,actor=self.user,expected_outcome='normal',**self.confirmation(booking))

    def test_rescheduling_to_same_start_is_rejected_without_consuming_attempt(self):
        from studio.portal_services import member_reschedule
        original_slot=self.slot()
        booking=self.book(original_slot)
        with self.assertRaises(BusinessError):
            member_reschedule(booking.pk,actor=self.user,slot_id=original_slot.pk,expected_slot=slot_stamp(original_slot),**self.confirmation(booking,'reschedule'))
        booking.refresh_from_db()
        self.assertEqual(booking.status,'pending')
        self.client.force_login(self.user)
        response=self.client.get(f'/portal/bookings/{booking.pk}/reschedule/')
        self.assertNotIn(original_slot.pk,[item['slot'].pk for item in response.context['options']])

    def test_reschedule_after_start_rejected(self):
        from studio.portal_services import member_reschedule
        booking = self.book()
        target = self.slot(start=self.start+timedelta(days=2))
        with patch('django.utils.timezone.now',return_value=self.start):
            with self.assertRaises(BusinessError):
                member_reschedule(booking.pk,actor=self.user,slot_id=target.pk,expected_slot=slot_stamp(target),**self.confirmation(booking,'reschedule'))

    def test_cancel_and_reschedule_pages_require_confirm_checkbox(self):
        from studio.models import MemberRuleConfirmation
        booking=self.book()
        target=self.slot(start=self.start+timedelta(days=2))
        self.client.force_login(self.user)
        for action in ('cancel', 'reschedule'):
            url=f'/portal/bookings/{booking.pk}/{action}/'
            page=self.client.get(url)
            self.assertEqual(page.status_code,200)
            self.assertContains(page,'role="dialog"')
            self.assertContains(page,'我已阅读并同意以上规则')
            form=page.context['form']
            data=dict(form.initial)
            if action=='reschedule': data['slot_selection']=f'{target.pk}|{slot_stamp(target)}'
            response=self.client.post(url,data)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'勾选确认规则')
            booking.refresh_from_db()
            self.assertEqual(booking.status,'pending')
        self.assertFalse(MemberRuleConfirmation.objects.exists())

    def test_cancel_page_confirms_server_outcome_and_records_rule(self):
        from studio.models import MemberRuleConfirmation
        from studio.portal_rules import RULES
        booking=self.book()
        self.client.force_login(self.user)
        url=f'/portal/bookings/{booking.pk}/cancel/'
        page=self.client.get(url)
        self.assertContains(page,'本次取消不扣课时')
        data=dict(page.context['form'].initial,confirmed='on')
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,302)
        confirmation=MemberRuleConfirmation.objects.get(booking=booking)
        self.assertEqual(confirmation.rule_text,RULES['cancel']['text'])
        self.assertEqual(confirmation.user_id,self.user.pk)

    def test_reschedule_page_records_confirmation_and_locks_followup(self):
        from studio.models import MemberRuleConfirmation
        booking=self.book()
        target=self.slot(start=self.start+timedelta(days=2))
        self.client.force_login(self.user)
        url=f'/portal/bookings/{booking.pk}/reschedule/'
        page=self.client.get(url)
        self.assertContains(page,'改期成功后不可再次自主改期')
        response=self.client.post(url,dict(page.context['form'].initial,confirmed='on',slot_selection=f'{target.pk}|{slot_stamp(target)}'))
        self.assertEqual(response.status_code,302)
        confirmation=MemberRuleConfirmation.objects.get(booking=booking,action='reschedule')
        new=confirmation.result_booking
        self.assertEqual(response.url,f'/portal/bookings/{new.pk}/')
        self.assertContains(self.client.get(response.url),'该课程已经改期')
        self.assertEqual(self.client.post(f'/portal/bookings/{new.pk}/cancel/',{}).status_code,302)
        self.assertEqual(self.client.post(f'/portal/bookings/{new.pk}/reschedule/',{}).status_code,302)
        new.refresh_from_db()
        self.assertEqual(new.status,'pending')

    def test_stale_confirmation_cannot_charge_after_midnight(self):
        from studio.portal_services import member_cancel
        booking=self.book()
        now=timezone.localtime(booking.starts_at).replace(hour=0,minute=0,second=0,microsecond=0)
        with patch('django.utils.timezone.now',return_value=now):
            with self.assertRaises(BusinessError):
                member_cancel(booking.pk,actor=self.user,expected_outcome='normal',**self.confirmation(booking))
        booking.refresh_from_db()
        self.assertEqual(booking.status,'pending')
        self.assertFalse(LessonEntry.objects.filter(booking=booking).exists())

    def test_inactive_or_unbound_member_cannot_use_services(self):
        from studio.portal_services import member_book
        slot=self.slot()
        self.member.status=Member.Status.INACTIVE
        self.member.save(update_fields=['status'])
        with self.assertRaises(PermissionDenied): member_book(actor=self.user,slot_id=slot.pk,expected_slot=slot_stamp(slot),request_id=uuid.uuid4())
        self.member.status=Member.Status.ACTIVE
        self.member.save(update_fields=['status'])
        MemberAccount.objects.filter(user=self.user).delete()
        with self.assertRaises(PermissionDenied): member_book(actor=self.user,slot_id=slot.pk,expected_slot=slot_stamp(slot),request_id=uuid.uuid4())

    def test_rule_version_change_requires_reconfirmation(self):
        from studio.portal_services import member_cancel
        from studio.models import MemberRuleConfirmation
        booking=self.book()
        opts=self.confirmation(booking)
        opts['rule_version']='outdated-rule'
        with self.assertRaises(BusinessError):
            member_cancel(booking.pk,actor=self.user,expected_outcome='normal',**opts)
        booking.refresh_from_db()
        self.assertEqual(booking.status,'pending')
        self.assertFalse(MemberRuleConfirmation.objects.exists())

    def test_new_request_cannot_record_a_second_cancellation(self):
        from studio.portal_services import member_cancel
        from studio.models import MemberRuleConfirmation
        booking=self.book()
        member_cancel(booking.pk,actor=self.user,expected_outcome='normal',**self.confirmation(booking))
        with self.assertRaises(BusinessError):
            member_cancel(booking.pk,actor=self.user,expected_outcome='normal',**self.confirmation(booking))
        self.assertEqual(MemberRuleConfirmation.objects.filter(booking=booking,action='cancel').count(),1)

    def test_state_changes_require_csrf_and_other_methods_do_not_mutate(self):
        booking=self.book()
        client=Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for suffix in ('cancel/', 'reschedule/'):
            url=f'/portal/bookings/{booking.pk}/{suffix}'
            self.assertEqual(client.post(url,{}).status_code,403)
            self.assertEqual(client.put(url,{}).status_code,403)
            self.client.force_login(self.user)
            self.assertEqual(self.client.put(url,{}).status_code,405)
        self.assertEqual(client.post(f'/portal/courses/{self.slot(start=self.start+timedelta(days=4)).pk}/book/',{}).status_code,403)
        booking.refresh_from_db()
        self.assertEqual(booking.status,'pending')

    def test_staff_can_cancel_or_reschedule_member_locked_booking(self):
        from studio.portal_services import member_reschedule
        from studio.services import cancel_booking, reschedule_booking
        original=self.book()
        target=self.slot(start=self.start+timedelta(days=2))
        replacement=member_reschedule(original.pk,actor=self.user,slot_id=target.pk,expected_slot=slot_stamp(target),**self.confirmation(original,'reschedule'))
        later=reschedule_booking(replacement.pk,starts_at=self.start+timedelta(days=3),duration_minutes=60,
            actor=self.actor,reason='职员协助改期',request_id=uuid.uuid4())
        self.assertEqual(later.previous_booking_id,replacement.pk)
        cancelled=cancel_booking(later.pk,actor=self.actor,reason='职员协助取消')
        self.assertEqual(cancelled.status,'cancelled')


class MemberBookingConcurrencyTests(LessonFixtures, TransactionTestCase):
    def test_concurrent_members_cannot_overbook_one_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, connections
        from studio.models import AvailableSlot
        from studio.portal_services import member_book
        self.purchase(credits=2)
        users=[]
        first=get_user_model().objects.create_user('fictional_race_first')
        MemberAccount.objects.create(user=first,member=self.member)
        users.append(first.pk)
        second_member=Member.objects.create(name='虚构并发会员')
        self.purchase(member_id=second_member.pk,credits=2)
        second=get_user_model().objects.create_user('fictional_race_second')
        MemberAccount.objects.create(user=second,member=second_member)
        users.append(second.pk)
        slot=AvailableSlot.objects.create(course_type=self.course,coach=self.coach,
            starts_at=timezone.now()+timedelta(days=5),duration_minutes=60)
        barrier=Barrier(2)
        def attempt(user_id):
            close_old_connections()
            try:
                user=get_user_model().objects.get(pk=user_id)
                barrier.wait(timeout=5)
                try:
                    member_book(actor=user,slot_id=slot.pk,expected_slot=slot_stamp(slot),request_id=uuid.uuid4())
                    return 'ok'
                except BusinessError:
                    return 'rejected'
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results=list(executor.map(attempt,users))
        self.assertEqual(sorted(results),['ok','rejected'])
        self.assertEqual(Booking.objects.filter(slot=slot,status='pending').count(),1)
