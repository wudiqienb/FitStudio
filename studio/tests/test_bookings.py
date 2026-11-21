import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from studio.models import Member, Coach, Booking, BookingEvent, LessonEntry
from studio.services import (BusinessError, create_booking, reschedule_booking, cancel_booking,
    complete_booking, restore_booking, credit_balance, available_credits)
from .test_lessons import LessonFixtures


class BookingTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.start = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=3)

    def book(self, **kwargs):
        data = dict(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
                    starts_at=self.start, duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())
        data.update(kwargs)
        return create_booking(**data)

    def test_booking_reserves_available_but_does_not_deduct_ledger(self):
        booking = self.book()
        self.assertEqual(booking.status, Booking.Status.PENDING)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 9)
        self.assertEqual(LessonEntry.objects.count(), 1)

    def test_complete_deducts_once_and_records_event(self):
        booking = self.book()
        complete_booking(booking.pk, actor=self.actor)
        complete_booking(booking.pk, actor=self.actor)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)
        self.assertEqual(BookingEvent.objects.filter(action="complete").count(), 1)

    def test_insufficient_available_cannot_create(self):
        self.course.credits_cost = 11
        self.course.save()
        with self.assertRaises(BusinessError):
            self.book()
        self.assertFalse(Booking.objects.exists())

    def test_pending_bookings_cannot_overpromise_balance(self):
        self.course.credits_cost = 10
        self.course.save()
        self.book()
        with self.assertRaises(BusinessError):
            self.book(starts_at=self.start + timedelta(hours=2))

    def test_completion_checks_balance_again(self):
        booking = self.book()
        # 模拟导入/外部错误状态，验证最后一道不足保护；不修改真实数据库。
        Booking.objects.filter(pk=booking.pk).update(credits_cost=11)
        with self.assertRaises(BusinessError):
            complete_booking(booking.pk, actor=self.actor)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")
        self.assertEqual(LessonEntry.objects.count(), 1)

    def test_coach_conflict_with_other_member(self):
        self.book()
        other = Member.objects.create(name="虚构会员乙")
        self.purchase(member_id=other.pk)
        with self.assertRaises(BusinessError):
            self.book(member_id=other.pk, starts_at=self.start + timedelta(minutes=30))

    def test_member_conflict_with_other_coach(self):
        self.book()
        other = Coach.objects.create(name="虚构教练乙")
        with self.assertRaises(BusinessError):
            self.book(coach_id=other.pk, starts_at=self.start + timedelta(minutes=30))

    def test_adjacent_times_are_allowed(self):
        self.book()
        self.book(starts_at=self.start + timedelta(hours=1))
        self.assertEqual(Booking.objects.count(), 2)

    def test_cancel_releases_credits_and_time_without_deducting(self):
        booking = self.book(starts_at=timezone.now() + timedelta(days=2))
        cancel_booking(booking.pk, actor=self.actor, reason="虚构取消原因")
        cancel_booking(booking.pk, actor=self.actor, reason="重复请求")
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 10)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.book()
        self.assertEqual(BookingEvent.objects.filter(action="cancel").count(), 1)

    def test_reschedule_keeps_original_on_conflict(self):
        first = self.book()
        self.book(starts_at=self.start + timedelta(hours=2))
        with self.assertRaises(BusinessError):
            reschedule_booking(first.pk, starts_at=self.start + timedelta(hours=2), duration_minutes=60,
                               actor=self.actor, reason="虚构改期")
        first.refresh_from_db()
        self.assertEqual(first.starts_at, self.start)

    def test_reschedule_excludes_self_and_preserves_balance(self):
        first = self.book()
        changed = reschedule_booking(first.pk, starts_at=self.start + timedelta(minutes=15), duration_minutes=60,
                           actor=self.actor, reason="虚构改期")
        first.refresh_from_db()
        self.assertEqual(first.status, "rescheduled")
        self.assertEqual(changed.starts_at, self.start + timedelta(minutes=15))
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertEqual(first.events.filter(action="reschedule").count(), 1)

    def test_cancelled_cannot_complete_or_reschedule(self):
        first = self.book(starts_at=timezone.now() + timedelta(days=2))
        cancel_booking(first.pk, actor=self.actor, reason="虚构取消")
        with self.assertRaises(BusinessError):
            complete_booking(first.pk, actor=self.actor)
        with self.assertRaises(BusinessError):
            reschedule_booking(first.pk, starts_at=self.start, duration_minutes=60, actor=self.actor, reason="改期")

    def test_completed_cannot_cancel_or_reschedule(self):
        first = self.book()
        complete_booking(first.pk, actor=self.actor)
        with self.assertRaises(BusinessError):
            cancel_booking(first.pk, actor=self.actor, reason="取消")
        with self.assertRaises(BusinessError):
            reschedule_booking(first.pk, starts_at=self.start, duration_minutes=60, actor=self.actor, reason="改期")

    def test_future_lesson_cannot_be_completed_early(self):
        booking = self.book(starts_at=timezone.now() + timedelta(hours=1))
        with self.assertRaises(BusinessError):
            complete_booking(booking.pk, actor=self.actor)

    def test_cost_snapshot_does_not_follow_course_changes(self):
        booking = self.book()
        self.course.credits_cost = 5
        self.course.save()
        complete_booking(booking.pk, actor=self.actor)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 9)

    def test_create_request_is_idempotent_and_detects_changed_payload(self):
        key = uuid.uuid4()
        first = self.book(request_id=key)
        self.assertEqual(self.book(request_id=key).pk, first.pk)
        with self.assertRaises(BusinessError):
            self.book(request_id=key, duration_minutes=30)

    def test_restore_only_once_and_preserves_original_entry(self):
        booking = self.book()
        complete_booking(booking.pk, actor=self.actor)
        restore_booking(booking.pk, actor=self.actor, reason="虚构误扣纠正")
        restore_booking(booking.pk, actor=self.actor, reason="重复请求")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)
        self.assertEqual(LessonEntry.objects.filter(kind="restore").count(), 1)
        with self.assertRaises(BusinessError):
            complete_booking(booking.pk, actor=self.actor)

    def test_restore_requires_completed_and_reason(self):
        booking = self.book()
        with self.assertRaises(BusinessError):
            restore_booking(booking.pk, actor=self.actor, reason="误扣")
        complete_booking(booking.pk, actor=self.actor)
        with self.assertRaises(BusinessError):
            restore_booking(booking.pk, actor=self.actor, reason=" ")

    def test_unauthorized_completion_and_service_calls_denied(self):
        booking = self.book()
        user = get_user_model().objects.create_user(username="fictional_staff_booking", is_staff=True)
        with self.assertRaises(PermissionDenied):
            complete_booking(booking.pk, actor=user)
        with self.assertRaises(PermissionDenied):
            self.book(actor=user)

    def test_failure_between_ledger_and_state_rolls_back(self):
        booking = self.book()
        with patch("studio.services.BookingEvent.objects.create", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                complete_booking(booking.pk, actor=self.actor)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)

    def test_duration_and_naive_datetime_rejected(self):
        for kwargs in ({"duration_minutes": 0}, {"duration_minutes": 241}, {"duration_minutes": 30.5},
                       {"starts_at": self.start.replace(tzinfo=None)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BusinessError):
                self.book(**kwargs)

    def test_inactive_coach_cannot_be_booked(self):
        self.coach.status = "left"
        self.coach.save()
        with self.assertRaises(BusinessError):
            self.book()

    def test_course_default_duration_used_when_left_blank(self):
        self.course.duration_minutes = 45
        self.course.save()
        booking = self.book(duration_minutes=None)
        self.assertEqual(booking.ends_at-booking.starts_at, timedelta(minutes=45))
