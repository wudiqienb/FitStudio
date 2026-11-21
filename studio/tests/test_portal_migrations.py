from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class MemberAccountMigrationTests(TransactionTestCase):
    def test_0004_data_survives_account_table_migration(self):
        before_target = [('studio', '0004_lessonadjustment_and_more')]
        after_target = [('studio', '0005_memberaccount')]
        executor = MigrationExecutor(connection)
        executor.migrate(before_target)
        try:
            apps = executor.loader.project_state(before_target).apps
            User = apps.get_model('auth', 'User')
            Member = apps.get_model('studio', 'Member')
            Coach = apps.get_model('studio', 'Coach')
            Course = apps.get_model('studio', 'CourseType')
            Package = apps.get_model('studio', 'Package')
            Purchase = apps.get_model('studio', 'Purchase')
            Booking = apps.get_model('studio', 'Booking')
            Entry = apps.get_model('studio', 'LessonEntry')
            Event = apps.get_model('studio', 'BookingEvent')
            import uuid
            actor = User.objects.create(username='fictional_preportal_operator', password='!', is_staff=True)
            member = Member.objects.create(name='虚构旧会员', phone='虚构分机', notes='迁移前备注')
            Member.objects.create(name='虚构旧停用会员', status='inactive')
            coach = Coach.objects.create(name='虚构旧教练')
            course = Course.objects.create(name='虚构旧课程', duration_minutes=60, credits_cost=2)
            package = Package.objects.create(name='虚构旧套餐', course_type=course, credits=10, price='100.00')
            purchase = Purchase.objects.create(member=member, package=package, course_type=course,
                package_name=package.name, credits=10, amount='100.00', purchased_on=timezone.localdate(),
                reason='虚构旧购课', request_id=uuid.uuid4(), fingerprint='old-purchase', created_by=actor)
            Entry.objects.create(member=member, course_type=course, kind='purchase', delta=10,
                balance_before=0, balance_after=10, purchase=purchase, reason='虚构旧购课', created_by=actor)
            start = timezone.now() - timedelta(days=2)
            booking = Booking.objects.create(member=member, coach=coach, course_type=course,
                course_name=course.name, starts_at=start, ends_at=start+timedelta(hours=1),
                credits_cost=2, status='completed', fingerprint='old-booking', request_id=uuid.uuid4(),
                created_by=actor)
            Entry.objects.create(member=member, course_type=course, kind='complete', delta=-2,
                balance_before=10, balance_after=8, booking=booking, reason='虚构旧完成', created_by=actor)
            Event.objects.create(booking=booking, action='complete', reason='虚构旧完成',
                details={'before': 'pending', 'after': 'completed'}, created_by=actor)

            models = list(apps.get_app_config('studio').get_models()) + [User]

            def snapshot():
                return {model._meta.db_table: list(model.objects.order_by('pk').values()) for model in models}

            original = snapshot()
            MigrationExecutor(connection).migrate(after_target)
            self.assertEqual(original, snapshot())
            new_apps = MigrationExecutor(connection).loader.project_state(after_target).apps
            self.assertEqual(new_apps.get_model('studio', 'MemberAccount').objects.count(), 0)
            self.assertEqual(Member.objects.get(pk=member.pk).name, '虚构旧会员')
            self.assertEqual(Booking.objects.get(pk=booking.pk).credits_cost, 2)
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA integrity_check')
                self.assertEqual(cursor.fetchone()[0], 'ok')
                cursor.execute('PRAGMA foreign_key_check')
                self.assertEqual(cursor.fetchall(), [])
        finally:
            final = MigrationExecutor(connection)
            final.migrate(final.loader.graph.leaf_nodes())
