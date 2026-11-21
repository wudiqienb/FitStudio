import os
from logging import LogRecord
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.views.debug import get_exception_reporter_filter

from config.deployment_settings import parse_public_address
from config.reporting import RequestLogFilter


class DeploymentSettingsTests(TestCase):
    def test_public_address_adds_nonstandard_https_port_to_origin(self):
        self.assertEqual(
            parse_public_address("FitStudio.Localhost", "8443"),
            ("fitstudio.localhost", "https://fitstudio.localhost:8443"),
        )
        self.assertEqual(parse_public_address("studio.example.org", "443")[1],
                         "https://studio.example.org")

    def test_public_address_rejects_url_invalid_host_and_port(self):
        for domain, port in [
            ("https://studio.example.org", "443"), ("*.example.org", "443"),
            ("-bad.example.org", "443"), ("studio..example.org", "443"),
            ("fitstudio.localhost", "invalid"), ("fitstudio.localhost", "65536"),
        ]:
            with self.subTest(domain=domain, port=port):
                with self.assertRaises(ImproperlyConfigured):
                    parse_public_address(domain, port)

    @override_settings(
        DEBUG=False,
        DEFAULT_EXCEPTION_REPORTER_FILTER="config.reporting.PrivateRequestFilter",
    )
    def test_error_metadata_hides_query_and_referrer(self):
        request = RequestFactory().get("/members/?q=phase2-synthetic-marker",
                                      HTTP_REFERER="https://example.invalid/?q=synthetic")
        metadata = get_exception_reporter_filter(request).get_safe_request_meta(request)
        self.assertNotIn("phase2-synthetic-marker", str(metadata["QUERY_STRING"]))
        self.assertNotIn("synthetic", str(metadata["HTTP_REFERER"]))

    def test_production_django_log_uses_resolved_route_without_request_data(self):
        request = RequestFactory().get("/members/?q=phase2-synthetic-marker")
        record = LogRecord("django.request", 40, __file__, 1, "detail phase2-synthetic-marker", (), None)
        record.request = request
        record.status_code = 500
        record.exc_text = "raw phase2-synthetic-marker"
        self.assertTrue(RequestLogFilter().filter(record))
        self.assertEqual(record.getMessage(), "request method=GET route=<unresolved> status=500")
        self.assertIsNone(record.exc_text)


class HealthTests(TestCase):
    def test_health_is_public_and_uncached(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.head(reverse("healthz")).status_code, 200)
        self.assertEqual(self.client.post(reverse("healthz")).status_code, 405)

    def test_health_returns_safe_503_when_database_is_unavailable(self):
        with patch("config.health.connection.cursor", side_effect=OperationalError("internal detail")):
            response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})


class BootstrapAdminTests(TestCase):
    def setUp(self):
        self.output = StringIO()
        self.users = get_user_model().objects

    def bootstrap(self, username, answers, *, tty=True):
        with patch("studio.management.commands.bootstrap_admin.sys.stdin.isatty", return_value=tty):
            with patch("studio.management.commands.bootstrap_admin.getpass", side_effect=answers):
                call_command("bootstrap_admin", username, stdout=self.output)

    def test_password_is_hashed_and_existing_admin_is_untouched(self):
        password = "FitStudio-Test-Only-Bootstrap-Admin!"
        self.bootstrap("isolated-owner", [password, password])
        user = self.users.get(username="isolated-owner")
        self.assertTrue(user.is_active and user.is_staff and user.is_superuser)
        self.assertTrue(user.check_password(password))
        self.assertNotEqual(user.password, password)
        self.assertNotIn(password, self.output.getvalue())
        previous_hash = user.password
        with self.assertRaises(CommandError):
            self.bootstrap("isolated-owner", [])
        user.refresh_from_db()
        self.assertEqual(user.password, previous_hash)

    def test_noninteractive_mismatch_and_weak_password_are_refused(self):
        with patch.dict(os.environ, {"DJANGO_SUPERUSER_PASSWORD": "FitStudio-Test-Only-Ignored-Env!"}):
            with self.assertRaises(CommandError):
                self.bootstrap("isolated-owner", [], tty=False)
        for values in [("long synthetic one", "long synthetic two"), ("123", "123")]:
            with self.assertRaises(CommandError):
                self.bootstrap("isolated-owner", values)
        self.assertFalse(self.users.exists())

    def test_existing_username_is_never_promoted(self):
        self.users.create_user(username="isolated-member")
        with self.assertRaises(CommandError):
            self.bootstrap("isolated-member", [])
        self.assertFalse(self.users.get(username="isolated-member").is_staff)
