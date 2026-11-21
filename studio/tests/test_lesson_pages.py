import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.utils import timezone

from studio.models import Booking, LessonEntry, Purchase
from studio.services import create_booking
from .test_lessons import LessonFixtures


class LessonPageTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.booking = create_booking(member_id=self.member.pk, coach_id=self.coach.pk,
            course_type_id=self.course.pk, starts_at=timezone.now()-timedelta(hours=3),
            duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())

    def paths(self):
        return ["/catalog/", "/catalog/courses/add/", "/catalog/packages/add/", "/purchases/add/",
                f"/members/{self.member.pk}/lessons/", "/courses/", "/bookings/add/",
                f"/bookings/{self.booking.pk}/", f"/bookings/{self.booking.pk}/reschedule/",
                f"/bookings/{self.booking.pk}/complete/", f"/bookings/{self.booking.pk}/cancel/",
                f"/bookings/{self.booking.pk}/restore/"]

    def test_anonymous_must_login(self):
        for path in self.paths():
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 302)

    def test_pages_use_mobile_template(self):
        self.client.force_login(self.actor)
        for path in self.paths():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "studio/base.html")
                self.assertNotContains(response, "admin/css/base.css")

    def test_metadata_creation_and_purchase_post(self):
        self.client.force_login(self.actor)
        self.assertEqual(self.client.post("/catalog/courses/add/", {"name": "虚构拉伸", "credits_cost": 1,
            "duration_minutes": 45, "active": "on"}).status_code, 302)
        self.assertEqual(self.client.post("/catalog/packages/add/", {"name": "虚构5次卡", "course_type": self.course.pk,
            "credits": 5, "price": "400.00", "active": "on"}).status_code, 302)
        data = {"member": self.member.pk, "package": self.package.pk, "purchased_on": "2026-10-27",
                "reason": "虚构登记", "request_id": str(uuid.uuid4()), "credits": "3", "amount": "120.00"}
        self.assertEqual(self.client.post("/purchases/add/", data).status_code, 302)
        self.assertEqual(self.client.post("/purchases/add/", data).status_code, 302)
        self.assertEqual(Purchase.objects.count(), 2)  # 包括 setUp 的原始购课。

    def test_create_reschedule_cancel_posts(self):
        self.client.force_login(self.actor)
        starts = timezone.localtime(timezone.now()) + timedelta(days=1)
        data = {"member": self.member.pk, "coach": self.coach.pk, "course_type": self.course.pk,
                "day": starts.date().isoformat(), "start_time": "09:00", "duration_minutes": 60,
                "request_id": str(uuid.uuid4())}
        self.assertEqual(self.client.post("/bookings/add/", data).status_code, 302)
        self.assertEqual(self.client.post("/bookings/add/", data).status_code, 302)
        self.assertEqual(Booking.objects.count(), 2)
        booking = Booking.objects.exclude(pk=self.booking.pk).get()
        self.assertEqual(self.client.post(f"/bookings/{booking.pk}/reschedule/", {
            "day": starts.date().isoformat(), "start_time": "10:00", "duration_minutes": 45, "reason": "虚构改期"}).status_code, 302)
        booking.refresh_from_db()
        booking = booking.replacement
        confirmation = self.client.get(f"/bookings/{booking.pk}/cancel/")
        self.assertContains(confirmation, "正常取消")
        self.assertEqual(self.client.post(f"/bookings/{booking.pk}/cancel/", {"reason": "虚构取消", "expected_outcome": "normal", "expected_starts_at": booking.starts_at.isoformat()}).status_code, 302)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")

    def test_duplicate_completion_post_only_one_charge(self):
        self.client.force_login(self.actor)
        path = f"/bookings/{self.booking.pk}/complete/"
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 0)
        for _ in range(2):
            self.assertEqual(self.client.post(path, {"reason": "虚构确认完成"}).status_code, 302)
        self.assertEqual(LessonEntry.objects.filter(kind="complete").count(), 1)
        self.assertEqual(self.client.post(f"/bookings/{self.booking.pk}/restore/", {"reason": "虚构误扣纠正"}).status_code, 302)
        self.assertEqual(LessonEntry.objects.filter(kind="restore").count(), 1)

    def test_course_list_date_filter_and_invalid_date(self):
        self.client.force_login(self.actor)
        day = timezone.localdate(self.booking.starts_at).isoformat()
        self.assertContains(self.client.get("/courses/", {"date": day}), self.member.name)
        response = self.client.get("/courses/", {"date": "bad"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "日期格式")

    def test_csrf_and_unauthorized_completion(self):
        self.client.force_login(self.actor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.actor)
        self.assertEqual(client.post(f"/bookings/{self.booking.pk}/complete/", {"reason": "测试"}).status_code, 403)
        staff = get_user_model().objects.create_user(username="fictional_booking_viewer", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_booking"))
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/courses/").status_code, 200)
        self.assertEqual(self.client.post(f"/bookings/{self.booking.pk}/complete/", {"reason": "测试"}).status_code, 403)

    def test_history_admin_cannot_write_or_delete(self):
        self.client.force_login(self.actor)
        for model in ("purchase", "booking", "lessonentry", "bookingevent"):
            self.assertEqual(self.client.get(f"/admin/studio/{model}/").status_code, 200)
            self.assertEqual(self.client.post(f"/admin/studio/{model}/add/", {}).status_code, 403)
        self.assertEqual(self.client.post(f"/admin/studio/booking/{self.booking.pk}/change/", {}).status_code, 403)

    def test_member_with_history_cannot_be_deleted(self):
        self.client.force_login(self.actor)
        response = self.client.post(f"/members/{self.member.pk}/delete/", {"confirm": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Purchase.objects.filter(member=self.member).exists())
        self.assertTrue(LessonEntry.objects.filter(member=self.member).exists())
        self.member.refresh_from_db()

    def test_financial_fields_require_separate_view_permissions(self):
        staff = get_user_model().objects.create_user(username="fictional_course_only", is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename="view_coursetype"))
        self.client.force_login(staff)
        response = self.client.get("/catalog/")
        self.assertContains(response, self.course.name)
        self.assertNotContains(response, self.package.name)
        self.assertNotContains(response, "800.00")
        staff.user_permissions.add(Permission.objects.get(codename="view_lessonentry"))
        response = self.client.get(f"/members/{self.member.pk}/lessons/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "800.00")
