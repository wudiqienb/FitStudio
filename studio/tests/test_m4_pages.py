import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.utils import timezone

from studio.models import Booking, LessonEntry
from studio.services import create_booking, coach_cancel_booking
from .test_lessons import LessonFixtures


class FourthStagePages(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.start = timezone.now().replace(second=0, microsecond=0) + timedelta(days=2)
        self.booking = create_booking(member_id=self.member.pk, coach_id=self.coach.pk,
            course_type_id=self.course.pk, starts_at=self.start, duration_minutes=60,
            actor=self.actor, request_id=uuid.uuid4())

    def paths(self):
        return ["/courses/backfill/", "/adjustments/add/", f"/bookings/{self.booking.pk}/no-show/",
                f"/bookings/{self.booking.pk}/coach-cancel/", f"/bookings/{self.booking.pk}/makeup/"]

    def test_new_mobile_pages_require_login_and_render(self):
        for path in self.paths():
            self.assertEqual(self.client.get(path).status_code, 302)
        self.client.force_login(self.actor)
        for path in self.paths():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "studio/base.html")
                self.assertContains(response, 'name="viewport"')

    def test_cancel_confirmation_shows_free_and_submit_records_free(self):
        self.client.force_login(self.actor)
        path = f"/bookings/{self.booking.pk}/cancel/"
        response = self.client.get(path)
        self.assertContains(response, "正常取消")
        self.assertContains(response, "不扣课")
        self.assertEqual(LessonEntry.objects.filter(booking=self.booking).count(), 0)
        data = {"reason": "虚构取消", "expected_outcome": "normal", "expected_starts_at": self.booking.starts_at.isoformat()}
        self.assertEqual(self.client.post(path, data).status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, "cancelled")

    def test_cancel_crossing_midnight_requires_new_confirmation(self):
        self.client.force_login(self.actor)
        local_start = timezone.localtime(self.start, timezone.get_default_timezone())
        midnight = timezone.make_aware(local_start.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None),
                                       timezone.get_default_timezone())
        data = {"reason": "虚构迟取消", "expected_outcome": "normal", "expected_starts_at": self.booking.starts_at.isoformat()}
        with patch("studio.services.timezone.now", return_value=midnight):
            response = self.client.post(f"/bookings/{self.booking.pk}/cancel/", data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "重新")
        self.assertEqual(LessonEntry.objects.filter(booking=self.booking).count(), 0)

    def test_history_filter_keeps_inactive_booking_out_of_live_schedule(self):
        self.client.force_login(self.actor)
        coach_cancel_booking(self.booking.pk, actor=self.actor, reason="虚构取消")
        day = timezone.localdate(self.start).isoformat()
        self.assertNotContains(self.client.get("/courses/", {"date": day}), self.member.name)
        self.assertContains(self.client.get("/courses/", {"date": day, "history": "1"}), self.member.name)
        self.assertContains(self.client.get("/"), "待安排补课")

    def test_adjustment_and_historical_forms_post(self):
        self.client.force_login(self.actor)
        actual = timezone.localtime(timezone.now()-timedelta(days=1), timezone.get_default_timezone())
        data = {"member": self.member.pk, "course_type": self.course.pk, "delta": "2",
                "day": actual.date().isoformat(), "start_time": actual.strftime("%H:%M"),
                "reason": "虚构补录", "request_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post("/adjustments/add/", data).status_code, 302)
        self.assertEqual(self.client.post("/adjustments/add/", data).status_code, 302)
        self.assertEqual(LessonEntry.objects.filter(kind="adjustment").count(), 1)
        data.update(coach=self.coach.pk, duration_minutes=60, request_id=str(uuid.uuid4()))
        self.assertEqual(self.client.post("/courses/backfill/", data).status_code, 302)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)

    def test_deduction_actions_require_explicit_permissions_and_csrf(self):
        staff = get_user_model().objects.create_user(username="fictional_m4_viewer", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="change_booking"))
        self.client.force_login(staff)
        for path in ("/courses/backfill/", "/adjustments/add/", f"/bookings/{self.booking.pk}/no-show/",
                     f"/bookings/{self.booking.pk}/coach-cancel/", f"/bookings/{self.booking.pk}/makeup/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, {}).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        self.assertEqual(client.post(f"/bookings/{self.booking.pk}/no-show/", {"reason": "虚构"}).status_code, 403)

    def test_adjustment_history_admin_is_read_only(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get("/admin/studio/lessonadjustment/").status_code, 200)
        self.assertEqual(self.client.post("/admin/studio/lessonadjustment/add/", {}).status_code, 403)

    def test_purchase_records_actual_time_separately(self):
        self.client.force_login(self.actor)
        data = {"member": self.member.pk, "package": self.package.pk, "purchased_on": "2026-10-26", "purchased_time": "09:30", "reason": "虚构购课", "request_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post("/purchases/add/", data).status_code, 302)
        entry = LessonEntry.objects.filter(kind="purchase").latest("pk")
        local = timezone.localtime(entry.occurred_at, timezone.get_default_timezone())
        self.assertEqual((local.year, local.month, local.day, local.hour, local.minute), (2026, 10, 26, 9, 30))
        self.assertIsNotNone(entry.created_at)
