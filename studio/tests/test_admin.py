import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.test import Client, TestCase
from django.urls import reverse

from studio.models import Member, Coach


class AdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = secrets.token_urlsafe(24)  # 仅存在测试进程内存中。
        cls.admin = get_user_model().objects.create_superuser(
            username="fictional_test_admin", password=cls.password
        )
        cls.staff = get_user_model().objects.create_user(
            username="fictional_staff", is_staff=True
        )
        cls.member = Member.objects.create(name="虚构会员甲", phone="测试分机001")
        cls.coach = Coach.objects.create(name="虚构教练甲", specialty="力量基础")

    def urls_for(self, model):
        prefix = f"admin:studio_{model._meta.model_name}"
        obj = self.member if model is Member else self.coach
        return [reverse("admin:index"), reverse(prefix + "_changelist"),
                reverse(prefix + "_add"), reverse(prefix + "_change", args=[obj.pk]),
                reverse(prefix + "_delete", args=[obj.pk])]

    def test_anonymous_cannot_access_management(self):
        for model in (Member, Coach):
            for url in self.urls_for(model):
                with self.subTest(url=url):
                    self.assertRedirects(self.client.get(url),
                                         reverse("admin:login") + "?next=" + url)

    def test_nonstaff_cannot_access_admin(self):
        user = get_user_model().objects.create_user(username="fictional_nonstaff")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 302)

    def test_staff_without_permission_is_forbidden(self):
        self.client.force_login(self.staff)
        for model in (Member, Coach):
            for url in self.urls_for(model)[1:]:
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)

    def test_view_only_staff_can_view_but_cannot_edit_or_delete(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="view_member"))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("admin:studio_member_changelist")).status_code, 200)
        url = reverse("admin:studio_member_change", args=[self.member.pk])
        self.assertContains(self.client.get(url), "虚构会员甲")
        self.assertEqual(self.client.post(url, {"name": "篡改"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:studio_member_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:studio_member_delete", args=[self.member.pk]), {"post": "yes"}).status_code, 403)

    def test_builtin_login_with_csrf_then_logout(self):
        client = Client(enforce_csrf_checks=True)
        url = reverse("admin:login")
        client.get(url)
        response = client.post(url, {"username": self.admin.username,
            "password": self.password, "next": reverse("admin:index"),
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertRedirects(response, reverse("admin:index"))
        self.assertContains(client.get(reverse("admin:index")), "FitStudio")
        response = client.post(reverse("admin:logout"), {
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get(reverse("admin:index")).status_code, 302)

    def test_wrong_password_does_not_login(self):
        response = self.client.post(reverse("admin:login"), {
            "username": self.admin.username, "password": secrets.token_urlsafe(24)})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_csrf_required_for_mutations(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("admin:studio_member_add"),
                                     {"name": "虚构未授权提交"}).status_code, 403)

    def test_admin_pages_and_responsive_assets(self):
        self.client.force_login(self.admin)
        for model in (Member, Coach):
            for url in self.urls_for(model):
                with self.subTest(url=url):
                    response = self.client.get(url)
                    if model is Member and url.endswith("/delete/"):
                        self.assertEqual(response.status_code,403)
                        self.assertTrue(Member.objects.filter(pk=self.member.pk).exists())
                        continue
                    self.assertEqual(response.status_code, 200)
                    self.assertContains(response, 'name="viewport"')
                    self.assertContains(response, "studio/admin.css")
                    self.assertContains(response, "bootstrap-grid.min.css")
        self.client.logout()
        self.assertContains(self.client.get(reverse("admin:login")), "FitStudio")

    def test_create_edit_delete_both_archives_and_audit(self):
        self.client.force_login(self.admin)
        for model in (Member, Coach):
            with self.subTest(model=model.__name__):
                prefix = f"admin:studio_{model._meta.model_name}"
                data = {"name": "虚构新增档案", "phone": "", "status": "active", "notes": "测试档案"}
                data.update({"joined_on": "2026-10-27"} if model is Member else {"specialty": "基础训练"})
                self.assertRedirects(self.client.post(reverse(prefix + "_add"), data), reverse(prefix + "_changelist"))
                obj = model.objects.get(name=data["name"])
                self.assertTrue(LogEntry.objects.filter(object_id=str(obj.pk), action_flag=ADDITION,
                    content_type__model=model._meta.model_name).exists())
                data["name"] = "虚构已修改档案"
                data["status"] = "inactive" if model is Member else "left"
                self.assertEqual(self.client.post(reverse(prefix + "_change", args=[obj.pk]), data).status_code, 302)
                obj.refresh_from_db()
                self.assertEqual(obj.name, data["name"])
                self.assertEqual(obj.status, data["status"])
                self.assertTrue(LogEntry.objects.filter(object_id=str(obj.pk), action_flag=CHANGE,
                    content_type__model=model._meta.model_name).exists())
                delete_url = reverse(prefix + "_delete", args=[obj.pk])
                if model is Member:
                    self.assertEqual(self.client.get(delete_url).status_code,403)
                    self.assertEqual(self.client.post(delete_url, {"post":"yes"}).status_code,403)
                    self.assertTrue(model.objects.filter(pk=obj.pk).exists())
                    self.assertFalse(LogEntry.objects.filter(object_id=str(obj.pk),action_flag=DELETION,content_type__model='member').exists())
                    continue
                self.assertEqual(self.client.get(delete_url).status_code, 200)
                self.assertTrue(model.objects.filter(pk=obj.pk).exists())
                self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 302)
                self.assertFalse(model.objects.filter(pk=obj.pk).exists())
                self.assertTrue(LogEntry.objects.filter(object_id=str(obj.pk), action_flag=DELETION,
                    content_type__model=model._meta.model_name).exists())

    def test_search_filter_and_ordering(self):
        self.client.force_login(self.admin)
        for model in (Member, Coach):
            with self.subTest(model=model.__name__):
                extra = model.objects.create(name="虚构档案乙", phone="测试分机999",
                    status="inactive" if model is Member else "left")
                url = reverse(f"admin:studio_{model._meta.model_name}_changelist")
                for params in ({"q": "分机999"}, {"q": "虚构档案乙"}, {"status__exact": extra.status}):
                    result = self.client.get(url, params)
                    self.assertEqual(list(result.context["cl"].queryset), [extra])
                result = self.client.get(url, {"o": "1"})  # name 列，数据库排序。
                expected = list(model.objects.order_by("name", "-pk").values_list("pk", flat=True))
                self.assertEqual(list(result.context["cl"].queryset.values_list("pk", flat=True)), expected)

    def test_required_name_and_invalid_status_rejected(self):
        self.client.force_login(self.admin)
        for model in (Member, Coach):
            for value in ("", "   "):
                data = {"name": value, "status": "active", "joined_on": "2026-10-27"}
                response = self.client.post(reverse(f"admin:studio_{model._meta.model_name}_add"), data)
                with self.subTest(model=model.__name__, value=value):
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("name", response.context["adminform"].form.errors)
            response = self.client.post(reverse(f"admin:studio_{model._meta.model_name}_add"),
                {"name": "虚构无效状态", "status": "bad", "joined_on": "2026-10-27"})
            self.assertIn("status", response.context["adminform"].form.errors)

    def test_notes_are_escaped(self):
        self.member.notes = '<script>alert("fictional")</script>'
        self.member.save()
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin:studio_member_change", args=[self.member.pk]))
        self.assertNotContains(response, self.member.notes)
        self.assertContains(response, "&lt;script&gt;")
