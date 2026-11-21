from django.urls import path,include

from . import views, lesson_views, portal_views, quick_views

from . import balance_views

from . import booking_v2_views

app_name = "studio"
urlpatterns = [
    path("portal/reserve/", booking_v2_views.reserve, name="portal_reserve"),
    path("portal/reserve/submit/", booking_v2_views.submit, name="portal_reserve_submit"),
    path("portal/bookings/<int:pk>/move/", booking_v2_views.reserve, name="portal_move"),
    path("",include("studio.archive_urls")),
    path("",include("studio.credential_urls")),
    path("members/<int:pk>/balance/",balance_views.member_balance,name="member_balance"),
    path("quick/open/", quick_views.quick_open_view, name="quick_open"),
    path("quick/renew/", quick_views.quick_renew_view, name="quick_renew"),
    path("members/<int:pk>/credentials/reset/", quick_views.reset_credentials_view, name="member_credentials_reset"),
    path("portal/", portal_views.member_home, name="portal_home"),
    path("portal/login/", portal_views.MemberLoginView.as_view(), name="portal_login"),
    path("portal/logout/", portal_views.MemberLogoutView.as_view(), name="portal_logout"),
    path("portal/courses/", portal_views.courses, name="portal_courses"),
    path("portal/courses/<int:pk>/confirm/", portal_views.booking_confirmation, name="portal_booking_confirmation"),
    path("portal/courses/<int:pk>/book/", portal_views.book_slot, name="portal_book_slot"),
    path("portal/bookings/", portal_views.bookings, name="portal_bookings"),
    path("portal/bookings/<int:pk>/", portal_views.booking_detail, name="portal_booking_detail"),
    path("portal/bookings/<int:pk>/cancel/", portal_views.cancel, name="portal_cancel"),
    path("portal/bookings/<int:pk>/reschedule/", portal_views.reschedule, name="portal_reschedule"),
    path("", views.HomeView.as_view(), name="home"),
    path("courses/", lesson_views.CourseListView.as_view(), name="courses"),
    path("courses/backfill/", lesson_views.BookingBackfillView.as_view(), name="booking_backfill"),
    path("adjustments/add/", lesson_views.LessonAdjustmentView.as_view(), name="adjustment_add"),
    path("catalog/", lesson_views.CatalogView.as_view(), name="catalog"),
    path("catalog/courses/add/", lesson_views.CourseCreateView.as_view(), name="course_add"),
    path("catalog/packages/add/", lesson_views.PackageCreateView.as_view(), name="package_add"),
    path("purchases/add/", lesson_views.PurchaseCreateView.as_view(), name="purchase_add"),
    path("members/<int:pk>/lessons/", lesson_views.MemberLessonsView.as_view(), name="member_lessons"),
    path("bookings/add/", lesson_views.BookingCreateView.as_view(), name="booking_add"),
    path("bookings/<int:pk>/", lesson_views.BookingDetailView.as_view(), name="booking_detail"),
    path("bookings/<int:pk>/reschedule/", lesson_views.BookingRescheduleView.as_view(), name="booking_reschedule"),
    path("bookings/<int:pk>/makeup/", lesson_views.BookingMakeupView.as_view(), name="booking_makeup"),
    path("bookings/<int:pk>/<str:action>/", lesson_views.BookingActionView.as_view(), name="booking_action"),
    path("more/", views.MoreView.as_view(), name="more"),
    path("members/", views.MemberListView.as_view(), name="members"),
    path("members/<int:pk>/account/add/", views.MemberAccountCreateView.as_view(), name="member_account_add"),
    path("members/add/", views.MemberCreateView.as_view(), name="member_add"),
    path("members/<int:pk>/edit/", views.MemberUpdateView.as_view(), name="member_edit"),
    path("members/<int:pk>/delete/", views.MemberDeleteView.as_view(), name="member_delete"),
]
