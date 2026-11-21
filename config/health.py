from django.db import DatabaseError, connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe


@never_cache
@require_safe
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM django_migrations WHERE app = %s LIMIT 1", ["studio"])
            if cursor.fetchone():
                return JsonResponse({"status": "ok"})
    except DatabaseError:
        pass
    return JsonResponse({"status": "unavailable"}, status=503)
