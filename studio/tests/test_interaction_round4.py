import subprocess
from pathlib import Path
from django.conf import settings
from django.test import SimpleTestCase

class RequestTimeoutTests(SimpleTestCase):
    def check_transport(self, scenario):
        result = subprocess.run(['node', str(Path(__file__).with_name("test_transport.cjs")), scenario], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
    def test_success_requires_actual_json(self): self.check_transport('success')
    def test_connection_timeout_aborts_without_replay(self): self.check_transport('timeout')
    def test_body_timeout_aborts_without_replay(self): self.check_transport('body_timeout')
    def test_non_json_permission_error_is_not_success(self): self.check_transport('http')

    def test_toast_dedup_preserves_distinct_levels_and_dismiss(self): self.check_transport('toast_duplicate')
