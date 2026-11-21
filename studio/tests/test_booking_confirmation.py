import uuid
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from studio.models import AvailableSlot, Booking, Member, MemberAccount, LessonEntry
from studio.services import available_credits, credit_balance
from .test_lessons import LessonFixtures


class BookingConfirmationTests(LessonFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user('confirmation_test_member')
        MemberAccount.objects.create(user=self.user, member=self.member)
        self.purchase(credits=2)
        self.slot = AvailableSlot.objects.create(course_type=self.course, coach=self.coach,
            starts_at=timezone.now()+timedelta(days=5), duration_minutes=60)
        self.client.force_login(self.user)
        self.url = f'/portal/courses/{self.slot.pk}/confirm/'

    def confirmation(self):
        return self.client.get(self.url)

    def post_data(self, response):
        return {'request_id': str(response.context['request_id']), 'expected_slot': response.context['stamp'],
                'confirmation_token': response.context['confirmation_token']}

    def test_open_and_reconsider_are_read_only(self):
        response=self.confirmation()
        self.assertEqual(response.status_code,200)
        self.assertContains(response, 'role="dialog"')
        self.assertContains(response,self.course.name)
        self.assertContains(response,self.coach.name)
        self.assertContains(response,timezone.localtime(self.slot.starts_at).strftime('%Y年%m月%d日 %H:%M'))
        self.assertContains(response,timezone.localtime(self.slot.starts_at+timedelta(minutes=60)).strftime('%Y年%m月%d日 %H:%M'))
        self.assertContains(response,'再想想')
        self.assertContains(response,'不立即扣除实际余额')
        self.assertEqual(self.client.get('/portal/courses/').status_code,200)
        self.assertEqual(Booking.objects.count(),0)
        self.assertEqual((credit_balance(self.member.pk,self.course.pk),available_credits(self.member.pk,self.course.pk)),(2,2))

    def test_confirmation_requires_member_login_and_submission_requires_csrf(self):
        self.client.logout()
        self.assertEqual(self.confirmation().status_code,302)
        strict=Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        page=strict.get(self.url)
        self.assertEqual(strict.post(f'/portal/courses/{self.slot.pk}/book/',self.post_data(page)).status_code,403)
        self.assertFalse(Booking.objects.exists())

    def test_confirmation_token_is_required_and_bound_to_member_and_slot(self):
        page=self.confirmation()
        url=f'/portal/courses/{self.slot.pk}/book/'
        without={'request_id':str(page.context['request_id']),'expected_slot':page.context['stamp']}
        self.assertEqual(self.client.post(url,without).status_code,302)
        self.assertFalse(Booking.objects.exists())
        data=self.post_data(page)
        other=Member.objects.create(name='虚构其他会员')
        other_user=get_user_model().objects.create_user('confirmation_other_member')
        MemberAccount.objects.create(user=other_user,member=other)
        self.purchase(member_id=other.pk,credits=2)
        self.client.force_login(other_user)
        self.assertEqual(self.client.post(url,data).status_code,302)
        self.assertFalse(Booking.objects.exists())

    def test_confirm_and_repeat_reserve_only_once(self):
        data=self.post_data(self.confirmation())
        for _ in range(2):
            self.assertEqual(self.client.post(f'/portal/courses/{self.slot.pk}/book/',data).status_code,302)
        self.assertEqual(Booking.objects.filter(member=self.member,slot=self.slot).count(),1)
        self.assertEqual((credit_balance(self.member.pk,self.course.pk),available_credits(self.member.pk,self.course.pk)),(2,1))
        self.assertFalse(LessonEntry.objects.filter(booking__slot=self.slot).exists())

    def test_lost_slot_after_confirmation_is_rejected(self):
        data=self.post_data(self.confirmation())
        other=Member.objects.create(name='虚构另一会员')
        self.purchase(member_id=other.pk,credits=2)
        from studio.services import create_booking
        create_booking(member_id=other.pk,coach_id=self.coach.pk,course_type_id=self.course.pk,
            starts_at=self.slot.starts_at,duration_minutes=60,actor=self.actor,request_id=uuid.uuid4())
        response=self.client.post(f'/portal/courses/{self.slot.pk}/book/',data,follow=True)
        self.assertContains(response,'已满')
        self.assertFalse(Booking.objects.filter(member=self.member).exists())
        self.assertEqual(available_credits(self.member.pk,self.course.pk),2)

    def test_insufficient_credit_rejected_at_confirmation_submission(self):
        data=self.post_data(self.confirmation())
        from studio.services import adjust_credits
        adjust_credits(member_id=self.member.pk,course_type_id=self.course.pk,delta=-2,
            occurred_at=timezone.now(),actor=self.actor,request_id=uuid.uuid4(),reason='虚构测试')
        response=self.client.post(f'/portal/courses/{self.slot.pk}/book/',data,follow=True)
        self.assertContains(response,'可用课时不足')
        self.assertFalse(Booking.objects.filter(member=self.member).exists())

    def test_full_slot_cannot_open_confirmation(self):
        from studio.services import create_booking
        other=Member.objects.create(name='虚构已预约会员')
        self.purchase(member_id=other.pk,credits=2)
        create_booking(member_id=other.pk,coach_id=self.coach.pk,course_type_id=self.course.pk,
            starts_at=self.slot.starts_at,duration_minutes=60,actor=self.actor,request_id=uuid.uuid4())
        response=self.confirmation()
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.url,'/portal/courses/')
