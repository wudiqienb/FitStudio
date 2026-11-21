from django.urls import path

from .credential_views import change_password

urlpatterns = [path('portal/change-password/', change_password, name='portal_password_change')]
