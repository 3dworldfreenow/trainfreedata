"""Encryption and masking helpers for PNR data.

* PNR numbers are stored encrypted (Fernet / AES-128-CBC + HMAC-SHA256).
* A keyed HMAC-SHA256 hash of the PNR is stored for lookups / de-duplication.
* Only the last 4 digits are ever shown in the UI or API.
* Passenger names are never stored (sources' name fields are dropped during normalisation).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

PNR_RE = re.compile(r"^\d{10}$")


class Crypto:
    def __init__(self) -> None:
        self._fernet: Fernet | None = None
        self._hash_key: bytes = b""

    def init_app(self, app) -> None:
        key = app.config.get("DATA_ENCRYPTION_KEY", "")
        hkey = app.config.get("PNR_HASH_KEY", "")
        if not key or not hkey:
            raise RuntimeError("DATA_ENCRYPTION_KEY and PNR_HASH_KEY must be set (see .env.example)")
        try:
            raw = base64.urlsafe_b64decode(key)
            if len(raw) != 32:
                raise ValueError
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("DATA_ENCRYPTION_KEY must be a urlsafe-base64 encoded 32-byte key") from exc
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self._hash_key = hkey.encode()

    def encrypt(self, value: str) -> str:
        assert self._fernet is not None
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        assert self._fernet is not None
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, AttributeError):
            return None

    def encrypt_json(self, value: Any) -> str:
        return self.encrypt(json.dumps(value, sort_keys=True, default=str))

    def decrypt_json(self, token: str) -> Any:
        raw = self.decrypt(token)
        return json.loads(raw) if raw else None

    def pnr_hash(self, pnr: str) -> str:
        return hmac.new(self._hash_key, pnr.encode(), hashlib.sha256).hexdigest()


crypto = Crypto()


def normalize_pnr(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits if PNR_RE.match(digits) else None


def mask_pnr(last4: str | None) -> str:
    return f"******{last4}" if last4 else "**********"


def content_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
