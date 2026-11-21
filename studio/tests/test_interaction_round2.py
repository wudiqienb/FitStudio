import uuid
from datetime import timedelta
from django.test import TestCase, Client
from django.utils import timezone
from django.contrib.auth import get_user_model
from studio.models import Booking, LessonEntry
from studio.services import create_booking, complete_booking, coach_cancel_booking
from .test_lessons import LessonFixtures

class CoachInteractionTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.purchase()
        self.booking = create_booking(member_id=self.member.pk, coach_id=self.coach.pk, course_type_id=self.course.pk, starts_at=timezone.now()-timedelta(days=1), duration_minutes=60, actor=self.actor, request_id=uuid.uuid4())
        self.client.force_login(self.actor)

    def post(self, action, data=None):
        return self.client.post(f'/bookings/{self.booking.pk}/{action}/', data or {'reason':'测试确认'}, HTTP_X_FITSTUDIO_INTERACTION='1')

    def test_complete_success_and_balance(self):
        r=self.post('complete'); self.assertEqual(r.status_code,200)
        data=r.json(); self.assertTrue(data['ok']); self.assertEqual(data['account'], {'balance':9,'available':9})
        self.assertIn('已扣除1',data['message']); self.assertIn('已完成',data['html'])

    def test_complete_failure_is_real_and_no_deduction(self):
        self.booking.starts_at=timezone.now()+timedelta(days=1); self.booking.ends_at=self.booking.starts_at+timedelta(hours=1); self.booking.save()
        r=self.post('complete'); self.assertEqual(r.status_code,422); self.assertFalse(r.json()['ok']); self.assertIn('尚未结束',r.json()['message'])
        self.assertEqual(LessonEntry.objects.filter(booking=self.booking).count(),0)

    def test_repeat_complete_only_deducts_once(self):
        self.post('complete'); self.post('complete')
        self.assertEqual(LessonEntry.objects.filter(booking=self.booking,kind='complete').count(),1)
        self.assertEqual(self.post('complete').json()['account']['balance'],9)

    def test_no_show_feedback(self):
        data=self.post('no-show').json(); self.assertTrue(data['ok']); self.assertIn('已扣除1',data['message']); self.assertEqual(data['account']['balance'],9)

    def test_coach_cancel_no_deduction(self):
        data=self.post('coach-cancel').json(); self.assertIn('不扣课',data['message']); self.assertIn('待安排补课',data['html']); self.assertEqual(data['account']['balance'],10)

    def test_makeup_feedback_link_and_hold(self):
        coach_cancel_booking(self.booking.pk,actor=self.actor,reason='取消')
        start=timezone.localtime(timezone.now()+timedelta(days=3))
        data={'day':start.date().isoformat(),'start_time':start.strftime('%H:%M'),'duration_minutes':60,'coach':self.coach.pk,'request_id':str(uuid.uuid4()),'reason':'补课'}
        r=self.post('makeup',data); self.assertEqual(r.status_code,200); result=r.json(); self.assertIn('补课已安排',result['message']); self.assertEqual(result['account'],{'balance':10,'available':9})
        self.assertTrue(Booking.objects.filter(previous_booking=self.booking,makeup_origin=self.booking).exists())
        retry=self.post('coach-cancel').json(); self.assertIn('补课已安排',retry['message']); self.assertNotIn('待安排补课',retry['html'])

    def test_restore_uses_actual_ledger_balances(self):
        complete_booking(self.booking.pk,actor=self.actor)
        data=self.post('restore').json(); self.assertTrue(data['ok']); self.assertIn('冲正成功',data['message']); self.assertEqual(data['ledger'],{'before':9,'after':10,'delta':1}); self.assertEqual(data['account']['balance'],10)

    def test_restore_failure(self):
        r=self.post('restore'); self.assertEqual(r.status_code,422); self.assertFalse(r.json()['ok']); self.assertEqual(LessonEntry.objects.filter(kind='restore').count(),0)

    def test_form_validation_failure(self):
        r=self.post('complete',{'reason':''}); self.assertEqual(r.status_code,422); self.assertIn('reason',r.json()['errors'])

    def test_permission_unchanged(self):
        user=get_user_model().objects.create_user(username='limited',is_staff=True)
        self.client.force_login(user); self.assertEqual(self.post('complete').status_code,403)

    def test_csrf_unchanged(self):
        client=Client(enforce_csrf_checks=True); client.force_login(self.actor)
        self.assertEqual(client.post(f'/bookings/{self.booking.pk}/complete/',{'reason':'测试'},HTTP_X_FITSTUDIO_INTERACTION='1').status_code,403)

    def test_native_form_fallback(self):
        r=self.client.post(f'/bookings/{self.booking.pk}/complete/',{'reason':'测试'})
        self.assertEqual(r.status_code,302)

    def test_only_selected_forms_opt_in(self):
        self.assertContains(self.client.get(f'/bookings/{self.booking.pk}/complete/'),'data-fi-coach-form')
        self.assertNotContains(self.client.get('/bookings/add/'),'data-fi-coach-form')

    def test_adjustment_success_and_idempotent(self):
        now=timezone.localtime(timezone.now()-timedelta(days=1))
        data={'member':self.member.pk,'course_type':self.course.pk,'delta':2,'day':now.date().isoformat(),'start_time':now.strftime('%H:%M'),'reason':'补登记','request_id':str(uuid.uuid4())}
        for _ in range(2):
            r=self.client.post('/adjustments/add/',data,HTTP_X_FITSTUDIO_INTERACTION='1')
            self.assertTrue(r.json()['ok']); self.assertEqual(r.json()['ledger'],{'before':10,'after':12,'delta':2})
        self.assertEqual(LessonEntry.objects.filter(kind='adjustment').count(),1)

    def test_adjustment_failure_keeps_balance(self):
        now=timezone.localtime(timezone.now()-timedelta(days=1))
        data={'member':self.member.pk,'course_type':self.course.pk,'delta':-100,'day':now.date().isoformat(),'start_time':now.strftime('%H:%M'),'reason':'调整','request_id':str(uuid.uuid4())}
        r=self.client.post('/adjustments/add/',data,HTTP_X_FITSTUDIO_INTERACTION='1')
        self.assertEqual(r.status_code,422); self.assertFalse(r.json()['ok']); self.assertEqual(LessonEntry.objects.filter(kind='adjustment').count(),0)

    def test_member_cannot_use_staff_json(self):
        user=get_user_model().objects.create_user(username='not_staff')
        self.client.force_login(user); self.assertEqual(self.post('complete').status_code,403)
