FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-production.txt ./
RUN pip install --no-cache-dir -r requirements-production.txt \
    && useradd --create-home --user-group --uid 10001 fitstudio
RUN install -d -m 0700 -o 10001 -g 10001 /data \
    && install -m 0600 -o 10001 -g 10001 /dev/null /data/.fitstudio-volume
COPY manage.py ./
COPY config/ ./config/
COPY studio/ ./studio/
COPY templates/ ./templates/
COPY deploy/entrypoint.sh ./deploy/
RUN FITSTUDIO_SECRET_KEY="$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')" \
    FITSTUDIO_DOMAIN=example.invalid DJANGO_SETTINGS_MODULE=config.production_settings \
    python manage.py collectstatic --noinput
USER 10001:10001
ENTRYPOINT ["sh", "/app/deploy/entrypoint.sh"]
CMD ["gunicorn", "--config", "python:config.gunicorn", "config.wsgi:application"]
