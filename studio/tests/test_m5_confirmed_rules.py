"""经营者确认的改期与补课规则，全部使用隔离数据库及虚构资料。"""
import uuid
from datetime import timedelta
from django.test import TestCase
from studio.models import Booking, LessonEntry, Member
from studio.services import (BusinessError, cancel_booking, coach_cancel_booking,
    complete_booking, create_booking, create_makeup_booking, credit_balance,
    mark_no_show, reschedule_booking)
from .test_lessons import LessonFixtures
from .test_m4_rules import local_time
from unittest.mock import patch


class ConfirmedPolicies(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.original_start = local_time(2026, 10, 30, 18)
        self.original = create_booking(member_id=self.member.pk, coach_id=self.coach.pk,
            course_type_id=self.course.pk, starts_at=self.original_start, duration_minutes=60,
            actor=self.actor, request_id=uuid.uuid4())

    def replacement(self):
        with patch('studio.services.timezone.now', return_value=local_time(2026, 10, 30, 10)):
            return reschedule_booking(self.original.pk, starts_at=local_time(2026, 11, 1, 18),
                duration_minutes=60, actor=self.actor, request_id=uuid.uuid4(), reason='虚构改期')

    def test_rescheduled_booking_uses_new_date_for_free_cancel(self):
        replacement = self.replacement()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 10, 31, 23, 59, 59)):
            cancel_booking(replacement.pk, actor=self.actor, reason='虚构正常取消')
        self.original.refresh_from_db(); replacement.refresh_from_db()
        self.assertEqual((self.original.status, replacement.status), ('rescheduled', 'cancelled'))
        self.assertEqual(replacement.previous_booking_id, self.original.pk)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertFalse(LessonEntry.objects.filter(booking__in=[self.original, replacement]).exists())

    def test_rescheduled_booking_late_cancel_deducts_once(self):
        replacement = self.replacement()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 11, 1, 0)):
            cancel_booking(replacement.pk, actor=self.actor, reason='虚构提前通知')
            cancel_booking(replacement.pk, actor=self.actor, reason='虚构重复')
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, 'late_cancelled')
        self.assertEqual(LessonEntry.objects.filter(booking=replacement, kind='late_cancel').count(), 1)
        self.assertFalse(LessonEntry.objects.filter(booking=self.original).exists())
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)

    def test_reschedule_rejects_coach_conflict_and_keeps_original(self):
        other = Member.objects.create(name='虚构冲突会员')
        self.purchase(member_id=other.pk)
        create_booking(member_id=other.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
            starts_at=local_time(2026, 11, 1, 18), duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())
        with self.assertRaises(BusinessError):
            self.replacement()
        self.original.refresh_from_db()
        self.assertEqual(self.original.status, 'pending')
        self.assertFalse(Booking.objects.filter(previous_booking=self.original).exists())

    def test_old_rescheduled_booking_cannot_be_cancelled(self):
        self.replacement()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 10, 30, 10)), self.assertRaises(BusinessError):
            cancel_booking(self.original.pk, actor=self.actor, reason='虚构旧预约取消')

    def makeup(self):
        coach_cancel_booking(self.original.pk, actor=self.actor, reason='虚构教练取消')
        return create_makeup_booking(self.original.pk, starts_at=local_time(2026, 11, 2, 18),
            duration_minutes=60, coach_id=self.coach.pk, actor=self.actor, request_id=uuid.uuid4(), reason='虚构补课')

    def test_makeup_late_cancel_deducts_once_and_traces_origin(self):
        replacement = self.makeup()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 11, 2, 1)):
            cancel_booking(replacement.pk, actor=self.actor, reason='虚构提前通知')
            cancel_booking(replacement.pk, actor=self.actor, reason='虚构重复')
        self.assertEqual(replacement.makeup_origin_id, self.original.pk)
        self.assertEqual(LessonEntry.objects.filter(booking=replacement, kind='late_cancel').count(), 1)
        self.assertFalse(LessonEntry.objects.filter(booking=self.original).exists())
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        with self.assertRaises(BusinessError):
            complete_booking(replacement.pk, actor=self.actor)

    def test_makeup_no_show_deducts_once_and_traces_origin(self):
        replacement = self.makeup()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 11, 2, 18, 1)):
            mark_no_show(replacement.pk, actor=self.actor, reason='虚构未到场')
            mark_no_show(replacement.pk, actor=self.actor, reason='虚构重复')
        self.assertEqual(replacement.previous_booking_id, self.original.pk)
        self.assertEqual(LessonEntry.objects.filter(booking=replacement, kind='no_show').count(), 1)
        self.assertFalse(LessonEntry.objects.filter(booking=self.original).exists())
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)

    def test_makeup_complete_deducts_once(self):
        replacement = self.makeup()
        with patch('studio.services.timezone.now', return_value=local_time(2026, 11, 2, 20)):
            complete_booking(replacement.pk, actor=self.actor)
            complete_booking(replacement.pk, actor=self.actor)
        self.assertEqual(LessonEntry.objects.filter(booking=replacement, kind='complete').count(), 1)
        self.assertFalse(LessonEntry.objects.filter(booking=self.original).exists())
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)

    def test_adjustment_page_marks_member_navigation_current(self):
        self.client.force_login(self.actor)
        response = self.client.get('/adjustments/add/')
        self.assertContains(response, 'href="/members/" aria-current="page"', html=False)
