from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any


PASSWORD_ITERATIONS = 310_000
SESSION_TTL_SECONDS = 12 * 60 * 60


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(digest, _b64decode(expected))
    except (TypeError, ValueError):
        return False


def create_api_token() -> str:
    return secrets.token_urlsafe(32)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session_token(
    secret: str,
    username: str,
    session_version: int,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> str:
    payload = {
        "sub": username,
        "ver": session_version,
        "exp": int(time.time()) + ttl_seconds,
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def decode_session_token(secret: str, token: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
