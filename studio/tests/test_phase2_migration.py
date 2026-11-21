import uuid
from datetime import timedelta
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class Phase2MigrationTests(TransactionTestCase):
    def test_0005_to_0006_preserves_existing_accounts_bookings_and_ledger(self):
        previous=[('studio','0005_memberaccount')]
        latest=[('studio','0006_memberruleconfirmation_availableslot_booking_slot_and_more')]
        executor=MigrationExecutor(connection)
        executor.migrate(previous)
        try:
            apps=executor.loader.project_state(previous).apps
            User=apps.get_model('auth','User')
            Member=apps.get_model('studio','Member')
            Coach=apps.get_model('studio','Coach')
            Course=apps.get_model('studio','CourseType')
            Booking=apps.get_model('studio','Booking')
            Binding=apps.get_model('studio','MemberAccount')
            user=User.objects.create(username='fictional_migration_account',password='!')
            member=Member.objects.create(name='迁移测试会员',notes='保留原始备注')
            Binding.objects.create(user=user,member=member)
            coach=Coach.objects.create(name='迁移测试教练')
            course=Course.objects.create(name='迁移测试课程',credits_cost=2)
            start=timezone.now()+timedelta(days=4)
            Booking.objects.create(member=member,coach=coach,course_type=course,course_name=course.name,
                starts_at=start,ends_at=start+timedelta(hours=1),credits_cost=2,status='pending',
                request_id=uuid.uuid4(),fingerprint='existing',created_by=user)
            original_models=list(apps.get_app_config('studio').get_models())+[User]
            def snapshot():
                return {model._meta.db_table:list(model.objects.order_by('pk').values()) for model in original_models}
            before=snapshot()
            MigrationExecutor(connection).migrate(latest)
            self.assertEqual(before,snapshot())
            new_apps=MigrationExecutor(connection).loader.project_state(latest).apps
            self.assertEqual(new_apps.get_model('studio','AvailableSlot').objects.count(),0)
            self.assertEqual(new_apps.get_model('studio','MemberRuleConfirmation').objects.count(),0)
            self.assertIsNone(new_apps.get_model('studio','Booking').objects.get().slot_id)
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA integrity_check')
                self.assertEqual(cursor.fetchone()[0],'ok')
                cursor.execute('PRAGMA foreign_key_check')
                self.assertEqual(cursor.fetchall(),[])
        finally:
            final = MigrationExecutor(connection)
            final.migrate(final.loader.graph.leaf_nodes())
