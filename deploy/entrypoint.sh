#!/bin/sh
set -eu
umask 077

if [ "${1:-}" = "gunicorn" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
