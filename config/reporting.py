from logging import Filter

from django.views.debug import SafeExceptionReporterFilter


class PrivateRequestFilter(SafeExceptionReporterFilter):
    def get_safe_request_meta(self, request):
        metadata = super().get_safe_request_meta(request)
        for key in ("QUERY_STRING", "HTTP_REFERER"):
            if key in metadata:
                metadata[key] = self.cleansed_substitute
        return metadata


class RequestLogFilter(Filter):
    def filter(self, record):
        request = getattr(record, "request", None)
        if request is not None:
            route = getattr(getattr(request, "resolver_match", None), "route", None)
            record.msg = "request method=%s route=%s status=%s"
            record.args = (request.method, route or "<unresolved>", getattr(record, "status_code", "-"))
            record.exc_info = record.exc_text = record.stack_info = None
        return True
