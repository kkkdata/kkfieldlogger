from __future__ import annotations

import secrets
import string

from passlib.context import CryptContext

password_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
API_KEY_ALPHABET = string.ascii_letters + string.digits + "-_"
API_KEY_MIN_LENGTH = 12
API_KEY_DEFAULT_BYTES = 16


def hash_password(password: str) -> str:
    return password_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_context.verify(password, password_hash)


def generate_api_key(token_bytes: int = API_KEY_DEFAULT_BYTES) -> str:
    return secrets.token_urlsafe(token_bytes)


def normalize_api_key(value: str) -> str:
    normalized = (value or "").strip()
    if len(normalized) < API_KEY_MIN_LENGTH:
        raise ValueError(f"API key must be at least {API_KEY_MIN_LENGTH} characters")
    if any(char not in API_KEY_ALPHABET for char in normalized):
        raise ValueError("API key can only contain letters, numbers, hyphen, and underscore")
    return normalized


def generate_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))
