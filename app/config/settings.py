import os
from functools import lru_cache
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()


class Settings:
    supabase_url: str = os.environ["SUPABASE_URL"]
    supabase_anon_key: str = os.environ["SUPABASE_ANON_KEY"]
    supabase_service_role_key: str = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    frontend_url: str = os.environ.get("FRONTEND_URL", "http://localhost:5173")
    port: int = int(os.environ.get("PORT", 4000))

    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = int(os.environ.get("SMTP_PORT", 587))
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_pass: str = os.environ.get("SMTP_PASS", "")
    smtp_from: str = os.environ.get("SMTP_FROM", "")

    resend_api_key: str = os.environ.get("RESEND_API_KEY", "")

    emailjs_service_id: str = os.environ.get("EMAILJS_SERVICE_ID", "")
    emailjs_template_id: str = os.environ.get("EMAILJS_TEMPLATE_ID", "")
    emailjs_public_key: str = os.environ.get("EMAILJS_PUBLIC_KEY", "")
    emailjs_private_key: str = os.environ.get("EMAILJS_PRIVATE_KEY", "")

    otp_length: int = int(os.environ.get("OTP_LENGTH", 6))
    otp_expiration_minutes: int = int(os.environ.get("OTP_EXPIRATION_MINUTES", 10))
    otp_max_attempts: int = int(os.environ.get("OTP_MAX_ATTEMPTS", 5))

    datasets_bucket: str = os.environ.get("DATASETS_BUCKET", "datasets")


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_supabase_admin() -> Client:
    """Cliente con la service_role key. Solo se usa en el backend."""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


@lru_cache
def get_supabase_anon() -> Client:
    """Cliente con la anon key, respeta RLS. Se usa para validar login."""
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_anon_key)