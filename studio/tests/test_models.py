from datetime import timedelta

from django.apps import apps
from django.test import SimpleTestCase
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from studio.models import Member, Coach


class StudioInstallationTests(SimpleTestCase):
    def test_studio_app_is_installed(self):
        self.assertTrue(apps.is_installed("studio"))


class ArchiveModelTests(TestCase):
    def test_defaults_optional_fields_and_chinese_labels(self):
        for model, label in ((Member, "会员"), (Coach, "教练")):
            with self.subTest(model=model.__name__):
                obj = model(name="虚构测试档案")
                obj.full_clean()
                obj.save()
                self.assertEqual(obj.phone, "")
                self.assertEqual(obj.notes, "")
                self.assertEqual(obj.status, "active")
                self.assertEqual(str(obj), obj.name)
                self.assertEqual(model._meta.verbose_name, label)
                self.assertEqual(model._meta.get_field("name").verbose_name, "姓名")
                self.assertIsNotNone(obj.created_at)
                self.assertIsNotNone(obj.updated_at)
        self.assertEqual(Member(name="虚构会员").joined_on, timezone.localdate())
        self.assertEqual(Coach(name="虚构教练").specialty, "")

    def test_name_required_and_whitespace_rejected(self):
        for model in (Member, Coach):
            for name in (None, "", "   ", "\t"):
                with self.subTest(model=model.__name__, name=name):
                    with self.assertRaises(ValidationError):
                        model(name=name).full_clean()

    def test_phone_not_required_or_unique_or_mobile_only(self):
        for model in (Member, Coach):
            for index in range(2):
                obj = model(name=f"虚构档案{index}", phone="000-00000000 转 1")
                obj.full_clean()
                obj.save()

    def test_invalid_status_rejected_by_validation_and_database(self):
        for model in (Member, Coach):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError):
                    model(name="虚构档案", status="bad").full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    model.objects.create(name="虚构档案", status="bad")

    def test_field_length_and_invalid_date(self):
        for model in (Member, Coach):
            with self.subTest(model=model.__name__):
                for kwargs in ({"name": "甲" * 81}, {"name": "虚构档案", "phone": "1" * 33}):
                    with self.assertRaises(ValidationError):
                        model(**kwargs).full_clean()
        with self.assertRaises(ValidationError):
            Member(name="虚构会员", joined_on="not-a-date").full_clean()

    def test_updated_timestamp_and_creation_timestamp(self):
        for model in (Member, Coach):
            obj = model.objects.create(name="虚构档案")
            # 系统时钟可能在连续两次 save 之间不跳动，明确设置较早更新时间。
            model.objects.filter(pk=obj.pk).update(updated_at=timezone.now() - timedelta(days=1))
            obj.refresh_from_db()
            created, updated = obj.created_at, obj.updated_at
            obj.notes = "虚构更新"
            obj.save()
            obj.refresh_from_db()
            self.assertEqual(obj.created_at, created)
            self.assertGreater(obj.updated_at, updated)

    def test_deterministic_newest_first_order(self):
        for model in (Member, Coach):
            first = model.objects.create(name="虚构旧档案")
            second = model.objects.create(name="虚构新档案")
            self.assertEqual(list(model.objects.all()), [second, first])
