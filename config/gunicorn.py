bind = "0.0.0.0:8000"
workers = 2
timeout = 60
accesslog = "-"
errorlog = "-"

# U is the path. Do not include the request line, query, or request headers.
access_log_format = "%(m)s %(U)s %(s)s %(L)s"
