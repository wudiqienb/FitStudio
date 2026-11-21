import secrets

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.test import Client, TestCase
from django.urls import reverse

from studio.models import Member


class MobilePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(username="fictional_mobile_admin", password=None)
        cls.member = Member.objects.create(name="虚构手机会员", notes="虚构资料")
        cls.staff = get_user_model().objects.create_user(username="fictional_mobile_viewer", is_staff=True)
        cls.staff.user_permissions.add(Permission.objects.get(codename="view_member"))

    def test_mobile_routes_are_available(self):
        self.assertEqual(reverse("studio:home"), "/")

    def test_anonymous_must_login_for_all_core_pages(self):
        for path in ("/", "/courses/", "/more/", "/members/", "/members/add/",
                     f"/members/{self.member.pk}/edit/", f"/members/{self.member.pk}/delete/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.url.startswith("/admin/login/?next="))

    def test_nonstaff_and_staff_without_permission_are_forbidden(self):
        for staff in (False, True):
            user = get_user_model().objects.create_user(username=f"fictional_denied_{staff}", is_staff=staff)
            self.client.force_login(user)
            self.assertEqual(self.client.get("/members/").status_code, 403)
        self.client.force_login(get_user_model().objects.get(username="fictional_denied_False"))
        self.assertEqual(self.client.get("/").status_code, 403)

    def test_authorized_pages_use_custom_templates_and_bottom_navigation(self):
        self.client.force_login(self.admin)
        for path in ("/", "/courses/", "/more/", "/members/", "/members/add/",
                     f"/members/{self.member.pk}/edit/", f"/members/{self.member.pk}/delete/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                if path.endswith("/delete/"):
                    self.assertEqual(response.status_code,403)
                    self.assertTrue(Member.objects.filter(pk=self.member.pk).exists())
                    continue
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "studio/base.html")
                self.assertContains(response, 'aria-label="主导航"')
                self.assertContains(response, "bootstrap.min.css")
                self.assertNotContains(response, "admin/css/base.css")
                self.assertContains(response, 'name="viewport"')

    def test_home_shows_real_today_and_pending_context(self):
        self.client.force_login(self.admin)
        response = self.client.get("/")
        self.assertContains(response, "今日课程")
        self.assertContains(response, "待处理预约")
        self.assertIn("today_bookings", response.context)
        self.assertIn("pending_bookings", response.context)

    def test_member_cards_search_filter_pagination_and_invalid_query(self):
        self.client.force_login(self.admin)
        other = Member.objects.create(name="虚构停用会员", status="inactive", phone="测试分机777")
        for params in ({"q": "分机777"}, {"q": "虚构停用会员"}, {"status": "inactive"}):
            response = self.client.get("/members/", params)
            self.assertEqual(list(response.context["object_list"]), [other])
            self.assertNotContains(response, "<table")
        self.assertEqual(self.client.get("/members/", {"status": "bad"}).status_code, 400)
        self.assertEqual(self.client.get("/members/", {"page": "bad"}).status_code, 404)
        Member.objects.bulk_create([Member(name=f"虚构分页会员{i}") for i in range(15)])
        response = self.client.get("/members/")
        self.assertEqual(len(response.context["object_list"]), 12)
        self.assertTrue(response.context["page_obj"].has_next())

    def test_member_create_edit_delete_and_audit(self):
        self.client.force_login(self.admin)
        data = {"name": "虚构新增手机会员", "phone": "", "status": "active", "joined_on": "2026-10-27", "notes": ""}
        self.assertEqual(self.client.post("/members/add/", data).status_code, 302)
        obj = Member.objects.get(name=data["name"])
        data.update(name="虚构修改手机会员", status="inactive")
        self.assertEqual(self.client.post(f"/members/{obj.pk}/edit/", data).status_code, 302)
        obj.refresh_from_db()
        self.assertEqual(obj.status, "inactive")
        self.assertEqual(obj.name, data["name"])
        self.assertEqual(self.client.get(f"/members/{obj.pk}/delete/").status_code, 403)
        self.assertTrue(Member.objects.filter(pk=obj.pk).exists())
        self.assertEqual(self.client.post(f"/members/{obj.pk}/delete/", {"confirm": "yes"}).status_code, 403)
        self.assertTrue(Member.objects.filter(pk=obj.pk).exists())
        self.assertFalse(LogEntry.objects.filter(content_type__model="member",object_id=str(obj.pk),action_flag=DELETION).exists())
        for flag in (ADDITION, CHANGE):
            self.assertTrue(LogEntry.objects.filter(content_type__model="member", object_id=str(obj.pk), action_flag=flag).exists())

    def test_deletion_requires_explicit_confirmation(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(f"/members/{self.member.pk}/delete/", {}).status_code, 403)
        self.assertTrue(Member.objects.filter(pk=self.member.pk).exists())

    def test_view_only_account_cannot_mutate(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/members/").status_code, 200)
        self.assertNotContains(self.client.get("/members/"), 'href="/members/add/"')
        for path in ("/members/add/", f"/members/{self.member.pk}/edit/", f"/members/{self.member.pk}/delete/"):
            self.assertEqual(self.client.get(path).status_code, 403)
            self.assertEqual(self.client.post(path, {"name": "虚构尝试"}).status_code, 403)

    def test_form_validation_and_native_date_input(self):
        self.client.force_login(self.admin)
        form_page = self.client.get("/members/add/")
        self.assertContains(form_page, 'type="date"')
        self.assertContains(form_page, 'type="tel"')
        for data in ({"name": "  ", "status": "active", "joined_on": "2026-10-27"},
                     {"name": "虚构日期错误", "status": "active", "joined_on": "bad"}):
            response = self.client.post("/members/add/", data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)

    def test_mobile_csrf_and_text_escaping(self):
        self.client.force_login(self.admin)
        self.member.notes = "<script>fictional()</script>"
        self.member.save()
        response = self.client.get(f"/members/{self.member.pk}/edit/")
        self.assertNotContains(response, self.member.notes)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post("/members/add/", {}).status_code, 403)

    def test_native_admin_login_can_redirect_to_custom_home(self):
        password = secrets.token_urlsafe(24)
        self.admin.set_password(password)
        self.admin.save()
        response = self.client.post("/admin/login/?next=/", {"username": self.admin.username, "password": password, "next": "/"})
        self.assertRedirects(response, "/")

    def test_missing_member_returns_404(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/members/999999/edit/").status_code, 404)
