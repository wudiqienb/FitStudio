"""Probe the app locally without a proxy, redirect, or credential."""

import os
from http.client import HTTPConnection, HTTPException


def main():
    connection = HTTPConnection("127.0.0.1", 8000, timeout=3)
    try:
        connection.request(
            "GET",
            "/healthz/",
            headers={
                "Host": os.environ["FITSTUDIO_DOMAIN"].strip().lower(),
                "X-Forwarded-Proto": "https",
            },
        )
        response = connection.getresponse()
        return 0 if response.status == 200 and response.read(256).strip() == b'{"status": "ok"}' else 1
    except (OSError, HTTPException, KeyError):
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
