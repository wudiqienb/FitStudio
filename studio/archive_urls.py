from django.urls import path
from . import archive_views

urlpatterns = [
    path('members/archived/', archive_views.archived_members, name='archived_members'),
    path('members/<int:pk>/archive/', archive_views.member_archive, name='member_archive'),
    path('members/<int:pk>/archived/', archive_views.archived_member_detail, name='archived_member_detail'),
    path('members/<int:pk>/restore/', archive_views.member_restore, name='member_restore'),
]
