import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from studio.models import Member, Coach, CourseType, Package, Purchase, LessonEntry
from studio.services import BusinessError, register_purchase, credit_balance, available_credits, credit_snapshot


class LessonFixtures:
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(username="fictional_lessons", password=None)
        self.member = Member.objects.create(name="虚构会员甲")
        self.coach = Coach.objects.create(name="虚构教练甲")
        self.course = CourseType.objects.create(name="虚构力量私教", credits_cost=1)
        self.package = Package.objects.create(name="虚构10课套餐", course_type=self.course, credits=10, price=Decimal("800.00"))

    def purchase(self, **kwargs):
        data = dict(member_id=self.member.pk, package_id=self.package.pk, purchased_on=date(2026, 10, 27),
                    actor=self.actor, request_id=uuid.uuid4(), reason="虚构购课登记")
        data.update(kwargs)
        return register_purchase(**data)


class PurchaseTests(LessonFixtures, TestCase):
    def test_purchase_creates_snapshot_and_matching_entry(self):
        purchase = self.purchase()
        self.assertEqual(purchase.credits, 10)
        self.assertEqual(purchase.amount, Decimal("800.00"))
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 10)
        entry = LessonEntry.objects.get(purchase=purchase)
        self.assertEqual((entry.delta, entry.balance_before, entry.balance_after), (10, 0, 10))
        self.assertEqual(entry.created_by, self.actor)
        self.assertEqual(available_credits(self.member.pk, self.course.pk), 10)

    def test_package_changes_do_not_change_purchase_snapshot(self):
        purchase = self.purchase()
        self.package.name = "虚构修改套餐"
        self.package.credits = 99
        self.package.price = Decimal("999.00")
        self.package.save()
        purchase.refresh_from_db()
        self.assertEqual((purchase.package_name, purchase.credits, purchase.amount), ("虚构10课套餐", 10, Decimal("800.00")))

    def test_duplicate_purchase_key_does_not_add_credits_twice(self):
        key = uuid.uuid4()
        first = self.purchase(request_id=key)
        second = self.purchase(request_id=key)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(LessonEntry.objects.count(), 1)

    def test_same_key_different_payload_is_rejected(self):
        key = uuid.uuid4()
        self.purchase(request_id=key)
        with self.assertRaises(BusinessError):
            self.purchase(request_id=key, credits=3)

    def test_invalid_credits_amount_or_reason_rejected(self):
        for kwargs in ({"credits": 0}, {"credits": -1}, {"amount": Decimal("-1")},
                       {"amount": Decimal("1.001")}, {"amount": Decimal("NaN")}, {"reason": " "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BusinessError):
                self.purchase(**kwargs)
        self.assertEqual(Purchase.objects.count(), 0)

    def test_zero_amount_gift_and_course_type_balances_separate(self):
        self.purchase(credits=2, amount=Decimal("0"))
        other = CourseType.objects.create(name="虚构拉伸课程")
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 2)
        self.assertEqual(credit_balance(self.member.pk, other.pk), 0)

    def test_inactive_member_package_or_course_rejected(self):
        for obj, field, value in ((self.member, "status", "inactive"), (self.package, "active", False), (self.course, "active", False)):
            old = getattr(obj, field)
            setattr(obj, field, value)
            obj.save()
            with self.assertRaises(BusinessError):
                self.purchase()
            setattr(obj, field, old)
            obj.save()

    def test_purchase_and_entry_rollback_together(self):
        with patch("studio.services.LessonEntry.objects.create", side_effect=RuntimeError("test failure")):
            with self.assertRaises(RuntimeError):
                self.purchase()
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(credit_balance(self.member.pk, self.course.pk), 0)

    def test_history_protects_member_and_package(self):
        self.purchase()
        for obj in (self.member, self.package, self.course):
            with self.assertRaises(ProtectedError):
                obj.delete()

    def test_unauthorized_service_call_rejected(self):
        user = get_user_model().objects.create_user(username="fictional_denied")
        with self.assertRaises(PermissionDenied):
            self.purchase(actor=user)

    def test_balance_and_available_snapshot_use_one_select(self):
        self.purchase()
        with self.assertNumQueries(1):
            snapshot = credit_snapshot(self.member.pk, self.course.pk)
        self.assertEqual(snapshot, {"balance": 10, "available": 10})
