from django.test import SimpleTestCase, RequestFactory
from django.template.loader import render_to_string
from django.contrib.messages.storage.base import Message
from django.contrib.messages import constants

class InteractionTests(SimpleTestCase):
    def test_both_shells_load_shared_assets(self):
        for template in ('studio/base.html', 'portal/base.html'):
            html = render_to_string(template, {'request': RequestFactory().get('/')})
            self.assertIn('studio/interaction.css', html)
            self.assertIn('studio/interaction.js', html)
            self.assertIn('data-fi-toasts', html)

    def test_server_message_levels_are_preserved(self):
        for level, tag in ((constants.SUCCESS, 'success'), (constants.ERROR, 'error'), (constants.WARNING, 'warning'), (constants.INFO, 'info')):
            html = render_to_string('shared/feedback.html', {'messages': [Message(level, '服务端真实结果')]})
            self.assertIn('data-level="' + tag + '"', html)
            self.assertIn('服务端真实结果', html)

    def test_server_message_is_escaped(self):
        html = render_to_string('shared/feedback.html', {'messages': [Message(constants.ERROR, '<script>alert(1)</script>')]})
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_no_javascript_retains_message(self):
        html = render_to_string('shared/feedback.html', {'messages': [Message(constants.ERROR, '失败，请重试')]})
        self.assertIn('失败，请重试', html)
        self.assertNotIn('hidden', html)
        self.assertIn('role="alert"', html)
