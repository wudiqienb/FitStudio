from django.core.management.base import BaseCommand,CommandError
from django.contrib.auth import get_user_model
from studio.coach_roles import grant_daily_coach,DAILY_COACH_PERMISSIONS

class Command(BaseCommand):
    help='预览/增量配置教练日常业务组；默认只读，--apply才写入。'
    def add_arguments(self,parser):
        parser.add_argument('--user-id',type=int,required=True)
        parser.add_argument('--apply',action='store_true')
    def handle(self,*args,**options):
        try:user=get_user_model().objects.get(pk=options['user_id'])
        except get_user_model().DoesNotExist:raise CommandError('账号不存在。')
        if not user.is_active or not user.is_staff or user.is_superuser:raise CommandError('账号须为有效非超级工作人员。')
        self.stdout.write('日常业务权限：'+', '.join(DAILY_COACH_PERMISSIONS))
        self.stdout.write('保留现有直接权限及组；现有额外高风险授权须人工审查，不自动删除或扩大。')
        if options['apply']:
            grant_daily_coach(user);self.stdout.write('DAILY_COACH_INCREMENTAL_GRANT_OK')
        else:self.stdout.write('DRY_RUN：未修改授权。')
