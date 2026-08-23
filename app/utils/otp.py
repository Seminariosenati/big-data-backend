import random
import string
import bcrypt
from datetime import datetime, timedelta, timezone

from app.config.settings import get_settings


def generate_otp_code() -> str:
    settings = get_settings()
    return "".join(random.choices(string.digits, k=settings.otp_length))


def hash_otp(code: str) -> str:
    return bcrypt.hashpw(code.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def compare_otp(code: str, code_hash: str) -> bool:
    return bcrypt.checkpw(code.encode("utf-8"), code_hash.encode("utf-8"))


def get_otp_expiry() -> datetime:
    settings = get_settings()
    return datetime.now(timezone.utc) + timedelta(minutes=settings.otp_expiration_minutes)
