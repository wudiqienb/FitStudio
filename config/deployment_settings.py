import re

from django.core.exceptions import ImproperlyConfigured


def parse_public_address(domain, port):
    host = domain.strip().lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})*", host):
        raise ImproperlyConfigured("FITSTUDIO_DOMAIN must be a bare ASCII hostname")

    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ImproperlyConfigured("FITSTUDIO_HTTPS_PORT must be between 1 and 65535")

    suffix = f":{int(port)}" if int(port) != 443 else ""
    return host, f"https://{host}{suffix}"
