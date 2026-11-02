"""Application configuration, loaded from environment variables (.env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt PNR numbers and snapshots.
    DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY", "")
    # Separate key for the HMAC lookup hash of PNR numbers.
    PNR_HASH_KEY = os.getenv("PNR_HASH_KEY", "")

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", "mysql+pymysql://railmon:railmon@127.0.0.1:3306/railmon?charset=utf8mb4"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "pool_size": _int("DB_POOL_SIZE", 10),
        "max_overflow": _int("DB_MAX_OVERFLOW", 20),
    }

    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    # HTTP behaviour for outbound source requests
    HTTP_USER_AGENT = os.getenv(
        "HTTP_USER_AGENT", "RailMon/1.0 (self-hosted railway monitor; contact: admin@example.com)"
    )
    HTTP_TIMEOUT_SECONDS = _int("HTTP_TIMEOUT_SECONDS", 15)
    HTTP_MAX_RETRIES = _int("HTTP_MAX_RETRIES", 3)
    HTTP_BACKOFF_BASE_SECONDS = float(os.getenv("HTTP_BACKOFF_BASE_SECONDS", "1.5"))
    RESPECT_ROBOTS_TXT = _bool("RESPECT_ROBOTS_TXT", True)
    # Extra TLS 1.2 suites appended to OpenSSL defaults (www.indianrail.gov.in offers only these).
    HTTP_EXTRA_CIPHERS = os.getenv("HTTP_EXTRA_CIPHERS", "AES128-GCM-SHA256:AES256-GCM-SHA384")

    # Cache TTLs (seconds)
    CACHE_TTL_TRAIN_STATUS = _int("CACHE_TTL_TRAIN_STATUS", 240)
    CACHE_TTL_STATION_BOARD = _int("CACHE_TTL_STATION_BOARD", 240)
    CACHE_TTL_PNR = _int("CACHE_TTL_PNR", 600)
    CACHE_TTL_DISRUPTIONS = _int("CACHE_TTL_DISRUPTIONS", 3600)
    CACHE_TTL_STATIC = _int("CACHE_TTL_STATIC", 86400)

    # PNR monitoring limits
    PNR_MIN_CHECK_INTERVAL_MINUTES = _int("PNR_MIN_CHECK_INTERVAL_MINUTES", 30)
    CHART_MIN_CHECK_INTERVAL_MINUTES = _int("CHART_MIN_CHECK_INTERVAL_MINUTES", 15)
    PNR_MAX_CHECKS_PER_RUN = _int("PNR_MAX_CHECKS_PER_RUN", 20)
    PNR_MAX_SAVED_PER_USER = _int("PNR_MAX_SAVED_PER_USER", 25)
    PNR_RETENTION_DAYS_AFTER_JOURNEY = _int("PNR_RETENTION_DAYS_AFTER_JOURNEY", 7)
    PNR_ENQUIRIES_PER_USER_PER_HOUR = _int("PNR_ENQUIRIES_PER_USER_PER_HOUR", 30)

    LOG_RETENTION_DAYS = _int("LOG_RETENTION_DAYS", 30)
    LIVE_STATUS_RETENTION_DAYS = _int("LIVE_STATUS_RETENTION_DAYS", 90)
    DELAY_THRESHOLD_MINUTES = _int("DELAY_THRESHOLD_MINUTES", 15)
    STALE_AFTER_MINUTES = _int("STALE_AFTER_MINUTES", 15)

    # Public (non-PNR) pages can optionally require login.
    LOGIN_REQUIRED_FOR_PUBLIC_PAGES = _bool("LOGIN_REQUIRED_FOR_PUBLIC_PAGES", False)
    # Token for internal service-to-service REST calls (header: X-Internal-Token).
    INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")

    SCHEDULER_CONFIG_PATH = os.getenv("SCHEDULER_CONFIG_PATH", str(BASE_DIR / "config" / "scheduler.yaml"))
    SOURCES_CONFIG_PATH = os.getenv("SOURCES_CONFIG_PATH", str(BASE_DIR / "config" / "sources.yaml"))
    TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
    # Comma-separated Python modules with extra adapters (using @register_adapter)
    ADAPTER_MODULES = [m.strip() for m in os.getenv("ADAPTER_MODULES", "").split(",") if m.strip()]

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)


class TestConfig(Config):
    TESTING = True
    SECRET_KEY = "test-secret"
    DATA_ENCRYPTION_KEY = "Zm9yLXRlc3RzLW9ubHktMzItYnl0ZXMta2V5LTAwMDA="  # 32 bytes, tests only
    PNR_HASH_KEY = "test-hash-key"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    REDIS_URL = "fakeredis://"
    HTTP_MAX_RETRIES = 1
    HTTP_BACKOFF_BASE_SECONDS = 0.0
    RESPECT_ROBOTS_TXT = False
    INTERNAL_API_TOKEN = "test-internal-token"
