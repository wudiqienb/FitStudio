import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from studio.models import Booking, LessonEntry, Member, Purchase
from studio.services import BusinessError, create_booking, complete_booking, cancel_booking, credit_balance, credit_snapshot, restore_booking, mark_no_show
from .test_lessons import LessonFixtures


class SQLiteConcurrencyTests(LessonFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.creation.is_in_memory_db(connection.settings_dict["NAME"]):
            self.skipTest("并发必须用文件SQLite：--settings=config.test_settings")
        self.start = timezone.now().replace(second=0, microsecond=0) - timedelta(hours=3)
        self.purchase(credits=2)

    def book(self, **kwargs):
        values = dict(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk,
            starts_at=self.start, duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())
        values.update(kwargs)
        return create_booking(**values)

    def race(self, first, second):
        gate = Barrier(2)
        actor_id = self.actor.pk

        def worker(function):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=actor_id)
                gate.wait(timeout=5)
                try:
                    result = function(actor)
                    return "ok", result.pk
                except BusinessError as error:
                    return "rejected", error.messages
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, action) for action in (first, second)]
            return [future.result(timeout=15) for future in futures]

    def test_same_booking_simultaneous_completion_deducts_once(self):
        booking = self.book()
        action = lambda actor: complete_booking(booking.pk, actor=actor)
        results = self.race(action, action)
        self.assertEqual([r[0] for r in results], ["ok", "ok"])
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 1)

    def test_simultaneous_no_show_only_deducts_once(self):
        booking = self.book()
        action = lambda actor: mark_no_show(booking.pk, actor=actor, reason="虚构爽约")
        self.assertTrue(all(r[0] == "ok" for r in self.race(action, action)))
        self.assertEqual(LessonEntry.objects.filter(booking=booking, kind="no_show").count(), 1)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 1)

    def test_complete_and_no_show_race_only_one_deduction(self):
        booking = self.book()
        results = self.race(lambda actor: complete_booking(booking.pk, actor=actor),
                            lambda actor: mark_no_show(booking.pk, actor=actor, reason="虚构爽约"))
        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        booking.refresh_from_db()
        self.assertIn(booking.status, ["completed", "no_show"])
        self.assertEqual(LessonEntry.objects.filter(booking=booking, kind__in=["complete", "no_show"]).count(), 1)

    def test_two_bookings_simultaneous_completion_keeps_correct_balance(self):
        first = self.book()
        second = self.book(starts_at=self.start + timedelta(hours=1))
        results = self.race(lambda actor: complete_booking(first.pk, actor=actor),
                            lambda actor: complete_booking(second.pk, actor=actor))
        self.assertTrue(all(r[0] == "ok" for r in results))
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 0)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 2)

    def test_two_requests_for_coach_time_only_one_is_booked(self):
        other = Member.objects.create(name="虚构并发会员乙")
        self.purchase(member_id=other.pk)
        results = self.race(lambda actor: self.book(actor=actor), lambda actor: self.book(actor=actor, member_id=other.pk))
        self.assertEqual(sorted(r[0] for r in results), ["ok", "rejected"])
        self.assertEqual(Booking.objects.count(), 1)

    def test_duplicate_purchase_requests_only_one_entry(self):
        key = uuid.uuid4()
        action = lambda actor: self.purchase(actor=actor, request_id=key)
        results = self.race(action, action)
        self.assertTrue(all(r[0] == "ok" for r in results))
        self.assertEqual(Purchase.objects.filter(request_id=key).count(), 1)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 12)

    def test_complete_and_cancel_race_keep_status_and_entry_consistent(self):
        booking = self.book()
        self.race(lambda actor: complete_booking(booking.pk, actor=actor),
                  lambda actor: cancel_booking(booking.pk, actor=actor, reason="虚构取消"))
        booking.refresh_from_db()
        charged = LessonEntry.objects.filter(booking=booking, kind="complete").count()
        self.assertEqual(charged, 1 if booking.status == "completed" else 0)
        self.assertIn(booking.status, ["completed", "cancelled"])
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 2-charged)

    def test_actual_write_lock_timeout_fails_safely(self):
        booking = self.book()
        holder = sqlite3.connect(str(connection.settings_dict["NAME"]), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")

        def blocked():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA busy_timeout = 50")
                with self.assertRaises(BusinessError) as caught:
                    complete_booking(booking.pk, actor=actor)
                return str(caught.exception)
            finally:
                connections.close_all()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                error = pool.submit(blocked).result(timeout=5)
            self.assertIn("稍后重试", error)
        finally:
            holder.rollback()
            holder.close()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 2)

    def test_inconsistent_legacy_reservations_still_cannot_overdraw(self):
        first = self.book()
        second = self.book(starts_at=self.start + timedelta(hours=1))
        # 故障注入：模拟外部错误导入造成占用超过余额，检验完成时的最终保护。
        Booking.objects.filter(pk=second.pk).update(credits_cost=2)
        results = self.race(lambda actor: complete_booking(first.pk, actor=actor),
                            lambda actor: complete_booking(second.pk, actor=actor))
        # 超额预占属于异常旧数据，两次操作均拒绝，不能消耗其他预约的占用。
        self.assertEqual(sorted(r[0] for r in results), ["rejected", "rejected"])
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 2)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 0)

    def test_simultaneous_restore_only_one_positive_entry(self):
        booking = self.book()
        complete_booking(booking.pk, actor=self.actor)
        action = lambda actor: restore_booking(booking.pk, actor=actor, reason="虚构纠正")
        self.assertTrue(all(r[0] == "ok" for r in self.race(action, action)))
        self.assertEqual(LessonEntry.objects.filter(kind="restore").count(), 1)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 2)

    def test_simultaneous_duplicate_creation_returns_one_booking(self):
        key = uuid.uuid4()
        action = lambda actor: self.book(actor=actor, request_id=key)
        self.assertTrue(all(r[0] == "ok" for r in self.race(action, action)))
        self.assertEqual(Booking.objects.count(), 1)

    def test_credit_snapshot_stays_consistent_during_completion(self):
        booking = self.book()
        gate = Barrier(2)

        def reader():
            close_old_connections()
            try:
                gate.wait(timeout=5)
                return [credit_snapshot(self.member.pk, self.course.pk) for _ in range(25)]
            finally:
                connections.close_all()

        def writer():
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=self.actor.pk)
                gate.wait(timeout=5)
                complete_booking(booking.pk, actor=actor)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            read_future = pool.submit(reader)
            write_future = pool.submit(writer)
            snapshots = read_future.result(timeout=10)
            write_future.result(timeout=10)
        self.assertTrue(all((s["balance"], s["available"]) in [(2, 1), (1, 1)] for s in snapshots))
