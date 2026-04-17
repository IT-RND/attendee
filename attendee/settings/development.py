import os

from .base import *

DEBUG = True
SITE_DOMAIN = os.getenv("SITE_DOMAIN", "localhost:8000")
# DEBUG=True: accept any Host (port-forwards, LAN IPs, ngrok, etc.).
ALLOWED_HOSTS = ["*"]

# When TLS terminates in front of runserver (nginx, Cloudflare Tunnel, etc.),
# Django may see the request as HTTP while the browser sends Origin: https://…,
# which fails CSRF unless either this is set or CSRF_TRUSTED_ORIGINS includes
# that https origin (see below).
if os.getenv("USE_FORWARDED_HTTPS", "").lower() in ("1", "true", "yes"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

_csrf_origins = os.getenv("CSRF_TRUSTED_ORIGINS", "").strip()
if _csrf_origins:
    CSRF_TRUSTED_ORIGINS = [
        o.strip() for o in _csrf_origins.split(",") if o.strip()
    ]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "attendee_development",
        "USER": "attendee_development_user",
        "PASSWORD": "attendee_development_user",
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": "5432",
    }
}

# Log more stuff in development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "xmlschema": {"level": "WARNING", "handlers": ["console"], "propagate": False},
        # Uncomment to log database queries
        # "django.db.backends": {
        #    "handlers": ["console"],
        #    "level": "DEBUG",
        #    "propagate": False,
        # },
    },
}
