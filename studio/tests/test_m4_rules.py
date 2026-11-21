"""第四阶段：只使用虚构会员和教练；测试数据库由 Django 隔离创建。"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from studio.models import Booking, BookingEvent, LessonEntry
from studio.services import (BusinessError, available_credits, cancel_booking, coach_cancel_booking,
    complete_booking, create_booking, create_makeup_booking, credit_balance, mark_no_show,
    record_historical_lesson, reschedule_booking, restore_booking, adjust_credits)
from .test_lessons import LessonFixtures


def local_time(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=ZoneInfo("Asia/Shanghai"))


class FourthStageRules(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.start = local_time(2026, 10, 30, 18)

    def book(self, **changes):
        data = dict(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
                    starts_at=self.start, duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())
        data.update(changes)
        return create_booking(**data)

    def at(self, instant):
        return patch("studio.services.timezone.now", return_value=instant)

    def test_normal_cancel_at_previous_day_last_moment_is_free(self):
        booking = self.book()
        with self.at(local_time(2026, 10, 29, 23, 59, 59, 999999)):
            result = cancel_booking(booking.pk, actor=self.actor, reason="虚构正常取消")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 10)
        self.assertFalse(LessonEntry.objects.filter(booking=booking).exists())

    def test_midnight_late_cancel_deducts_once_even_if_informed(self):
        booking = self.book()
        with self.at(local_time(2026, 10, 30, 0)):
            first = cancel_booking(booking.pk, actor=self.actor, reason="虚构提前通知")
            second = cancel_booking(booking.pk, actor=self.actor, reason="重复提交")
        self.assertEqual((first.status, second.status), ("late_cancelled", "late_cancelled"))
        entry = LessonEntry.objects.get(booking=booking, kind="late_cancel")
        self.assertEqual(entry.delta, -1)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 9)
        self.assertEqual(BookingEvent.objects.filter(booking=booking, action="late_cancel").count(), 1)

    def test_too_late_to_cancel_after_course_starts(self):
        for instant in (self.start, self.start + timedelta(hours=2)):
            booking = self.book(starts_at=self.start + timedelta(days=Booking.objects.count()))
            with self.at(instant + timedelta(days=Booking.objects.count()-1)), self.assertRaises(BusinessError):
                cancel_booking(booking.pk, actor=self.actor, reason="虚构取消")

    def test_confirmation_result_changed_must_be_reviewed_again(self):
        booking = self.book()
        with self.at(local_time(2026, 10, 30, 0)), self.assertRaises(BusinessError):
            cancel_booking(booking.pk, actor=self.actor, reason="虚构取消", expected_outcome="normal")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")
        self.assertFalse(LessonEntry.objects.filter(booking=booking).exists())

    def test_no_show_after_start_deducts_once_and_is_distinct(self):
        booking = self.book()
        with self.at(self.start + timedelta(minutes=5)):
            mark_no_show(booking.pk, actor=self.actor, reason="虚构未到场")
            mark_no_show(booking.pk, actor=self.actor, reason="重复提交")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "no_show")
        self.assertEqual(LessonEntry.objects.get(booking=booking).kind, "no_show")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        self.assertEqual(BookingEvent.objects.filter(booking=booking, action="no_show").count(), 1)

    def test_no_show_before_start_or_after_completion_rejected(self):
        booking = self.book()
        with self.at(self.start - timedelta(seconds=1)), self.assertRaises(BusinessError):
            mark_no_show(booking.pk, actor=self.actor, reason="虚构未到场")
        with self.at(self.start + timedelta(hours=2)):
            complete_booking(booking.pk, actor=self.actor)
            with self.assertRaises(BusinessError):
                mark_no_show(booking.pk, actor=self.actor, reason="虚构未到场")

    def test_no_show_and_late_cancel_allow_audited_single_restore(self):
        for i, action in enumerate(("late_cancel", "no_show")):
            booking = self.book(starts_at=self.start + timedelta(days=i))
            if action == "late_cancel":
                with self.at(local_time(2026, 10, 30, 1)):
                    cancel_booking(booking.pk, actor=self.actor, reason="虚构迟取消")
            else:
                with self.at(booking.starts_at + timedelta(minutes=1)):
                    mark_no_show(booking.pk, actor=self.actor, reason="虚构爽约")
            restore_booking(booking.pk, actor=self.actor, reason="虚构误扣纠正")
            restore_booking(booking.pk, actor=self.actor, reason="重复请求")
            original = LessonEntry.objects.get(booking=booking, kind=action)
            reverse = LessonEntry.objects.get(booking=booking, kind="restore")
            self.assertEqual(reverse.reverses_id, original.pk)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)

    def test_coach_cancellation_and_makeup_complete_only_one_charge(self):
        original = self.book()
        coach_cancel_booking(original.pk, actor=self.actor, reason="虚构教练临时有事")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 10)
        when = self.start + timedelta(days=2)
        key = uuid.uuid4()
        replacement = create_makeup_booking(original.pk, starts_at=when, duration_minutes=60,
            coach_id=self.coach.pk, actor=self.actor, request_id=key, reason="虚构安排补课")
        same = create_makeup_booking(original.pk, starts_at=when, duration_minutes=60,
            coach_id=self.coach.pk, actor=self.actor, request_id=key, reason="虚构安排补课")
        self.assertEqual(same.pk, replacement.pk)
        self.assertEqual(replacement.previous_booking_id, original.pk)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 9)
        with self.at(when+timedelta(hours=2)):
            complete_booking(replacement.pk, actor=self.actor)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)

    def test_reschedule_creates_successor_and_no_duplicate_hold(self):
        original = self.book()
        when = self.start + timedelta(days=1)
        key = uuid.uuid4()
        changed = reschedule_booking(original.pk, starts_at=when, duration_minutes=60,
            actor=self.actor, reason="虚构改期", request_id=key)
        repeated = reschedule_booking(original.pk, starts_at=when, duration_minutes=60,
            actor=self.actor, reason="虚构改期", request_id=key)
        original.refresh_from_db()
        self.assertEqual(original.status, "rescheduled")
        self.assertNotEqual(original.pk, changed.pk)
        self.assertEqual(changed.previous_booking_id, original.pk)
        self.assertEqual(repeated.pk, changed.pk)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 9)
        with self.at(when + timedelta(hours=2)):
            complete_booking(changed.pk, actor=self.actor)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)

    def test_repeated_replacement_with_changed_payload_fails(self):
        original = self.book()
        key = uuid.uuid4()
        reschedule_booking(original.pk, starts_at=self.start+timedelta(days=1), duration_minutes=60,
                           actor=self.actor, reason="虚构改期", request_id=key)
        with self.assertRaises(BusinessError):
            reschedule_booking(original.pk, starts_at=self.start+timedelta(days=2), duration_minutes=60,
                               actor=self.actor, reason="虚构改期", request_id=key)

    def test_historical_lesson_records_actual_and_recorded_time_and_deduplicates(self):
        actual = local_time(2026, 10, 27, 9)
        recorded = local_time(2026, 10, 28, 10)
        key = uuid.uuid4()
        data = dict(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
                    starts_at=actual, duration_minutes=60, actor=self.actor, request_id=key, reason="虚构忘记登记")
        with self.at(recorded):
            first = record_historical_lesson(**data)
            second = record_historical_lesson(**data)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.starts_at, actual)
        self.assertEqual(first.created_at, recorded)
        entry = LessonEntry.objects.get(booking=first, kind="complete")
        self.assertEqual(entry.occurred_at, actual + timedelta(hours=1))
        self.assertEqual(entry.created_at, recorded)
        self.assertEqual(BookingEvent.objects.filter(booking=first, action="backfill").count(), 1)
        with self.at(recorded), self.assertRaises(BusinessError):
            record_historical_lesson(**dict(data, request_id=uuid.uuid4()))

    def test_historical_lesson_must_finish_and_not_spend_reserved_hours(self):
        past = local_time(2026, 10, 27, 9)
        future = local_time(2026, 11, 1, 18)
        self.course.credits_cost = 10
        self.course.save()
        self.book(starts_at=future)
        data = dict(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
                    starts_at=past, duration_minutes=60, actor=self.actor, request_id=uuid.uuid4(), reason="虚构补录")
        with self.at(local_time(2026, 10, 28, 10)), self.assertRaises(BusinessError):
            record_historical_lesson(**data)
        with self.at(past + timedelta(minutes=30)), self.assertRaises(BusinessError):
            record_historical_lesson(**dict(data, request_id=uuid.uuid4()))

    def test_late_cancellation_requires_distinct_permission(self):
        staff = get_user_model().objects.create_user(username="fictional_operator", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="change_booking"))
        booking = self.book()
        with self.at(local_time(2026, 10, 30, 1)), self.assertRaises(PermissionDenied):
            cancel_booking(booking.pk, actor=staff, reason="虚构迟取消")

    def test_credit_adjustment_is_audited_and_idempotent(self):
        key = uuid.uuid4()
        when = local_time(2026, 10, 26, 9)
        data = dict(member_id=self.member.pk, course_type_id=self.course.pk, delta=2,
                    occurred_at=when, reason="虚构课时补录", actor=self.actor, request_id=key)
        first = adjust_credits(**data)
        second = adjust_credits(**data)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 12)
        entry = LessonEntry.objects.get(adjustment=first)
        self.assertEqual((entry.kind, entry.delta, entry.occurred_at), ("adjustment", 2, when))
        with self.assertRaises(BusinessError):
            adjust_credits(**dict(data, delta=3))

    def test_negative_adjustment_cannot_spend_reserved_credits(self):
        self.course.credits_cost = 10
        self.course.save()
        self.book()
        with self.assertRaises(BusinessError):
            adjust_credits(member_id=self.member.pk, course_type_id=self.course.pk, delta=-1,
                occurred_at=timezone.now(), reason="虚构修正", actor=self.actor, request_id=uuid.uuid4())
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)

    def test_cross_year_cancellation_uses_studio_timezone(self):
        booking = self.book(starts_at=local_time(2027, 1, 1, 18))
        with timezone.override("America/New_York"), self.at(local_time(2026, 12, 31, 23, 59, 59)):
            cancelled = cancel_booking(booking.pk, actor=self.actor, reason="虚构跨年取消")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertFalse(LessonEntry.objects.filter(booking=booking).exists())

    def test_makeup_chain_after_reschedule_and_coach_cancel(self):
        original = self.book()
        coach_cancel_booking(original.pk, actor=self.actor, reason="虚构教练取消")
        makeup = create_makeup_booking(original.pk, starts_at=self.start+timedelta(days=2), duration_minutes=60,
            coach_id=self.coach.pk, actor=self.actor, request_id=uuid.uuid4(), reason="虚构补课")
        successor = reschedule_booking(makeup.pk, starts_at=self.start+timedelta(days=3), duration_minutes=60,
            actor=self.actor, request_id=uuid.uuid4(), reason="虚构改期")
        coach_cancel_booking(successor.pk, actor=self.actor, reason="虚构再次取消")
        final = create_makeup_booking(successor.pk, starts_at=self.start+timedelta(days=4), duration_minutes=60,
            coach_id=self.coach.pk, actor=self.actor, request_id=uuid.uuid4(), reason="虚构再次补课")
        self.assertEqual(final.makeup_origin_id, original.pk)
        self.assertEqual(final.previous_booking_id, successor.pk)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 9)
        self.assertFalse(LessonEntry.objects.filter(booking__in=[original, makeup, successor, final]).exists())

    def test_backfill_rejects_existing_late_cancel_deduction(self):
        booking = self.book()
        with self.at(local_time(2026, 10, 30, 1)):
            cancel_booking(booking.pk, actor=self.actor, reason="虚构迟取消")
        with self.at(self.start + timedelta(hours=2)), self.assertRaises(BusinessError):
            record_historical_lesson(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
                starts_at=self.start, duration_minutes=60, actor=self.actor, request_id=uuid.uuid4(), reason="虚构补录")
        self.assertEqual(LessonEntry.objects.filter(booking=booking, kind="late_cancel").count(), 1)
