"""
Autenticación de 2 factores basada en TOTP (RFC 6238), compatible con
Google Authenticator, Authy, 1Password, etc. — el mismo estándar que usa
GitHub para su "authenticator app" 2FA.

Flujo:
1. generate_totp_secret()          -> secreto base32 nuevo (aún no activo)
2. get_provisioning_uri()          -> URI otpauth:// para meter en el QR
3. generate_qr_code_data_uri()     -> PNG en base64 listo para <img src=...>
4. El usuario escanea el QR y escribe el código de 6 dígitos que le muestra
   su app -> verify_totp_code() confirma que el secreto y el reloj cuadran
   antes de marcar el 2FA como activo.
5. generate_recovery_codes()       -> códigos de un solo uso por si pierde
   el teléfono; se guardan hasheados (igual que una contraseña).
"""

import base64
import io
import secrets
import string

import bcrypt
import pyotp
import qrcode

from app.config.settings import get_settings


def generate_totp_secret() -> str:
    """Genera un secreto base32 nuevo, aleatorio, para un usuario."""
    return pyotp.random_base32()


def get_provisioning_uri(secret: str, email: str) -> str:
    """URI otpauth:// que la app autenticadora interpreta al escanear el QR."""
    settings = get_settings()
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.totp_issuer)


def generate_qr_code_data_uri(otpauth_url: str) -> str:
    """Genera el QR como PNG y lo devuelve como data URI, listo para el frontend."""
    img = qrcode.make(otpauth_url)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def verify_totp_code(secret: str, code: str) -> bool:
    """Valida un código de 6 dígitos contra el secreto guardado.

    valid_window=1 tolera hasta 30s de desfase de reloj hacia atrás/adelante,
    igual que hacen la mayoría de apps autenticadoras y servicios (GitHub,
    Google, etc.) para no ser demasiado estrictos con la hora del teléfono.
    """
    code = (code or "").strip().replace(" ", "")
    if not code or not code.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False


def generate_recovery_codes(count: int | None = None) -> list[str]:
    """Genera códigos de recuperación de un solo uso (formato XXXXX-XXXXX)."""
    settings = get_settings()
    total = count or settings.totp_recovery_codes_count
    alphabet = string.ascii_uppercase + string.digits
    codes = []
    for _ in range(total):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    normalized = code.strip().upper().replace(" ", "")
    return bcrypt.hashpw(normalized.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def compare_recovery_code(code: str, code_hash: str) -> bool:
    normalized = code.strip().upper().replace(" ", "")
    try:
        return bcrypt.checkpw(normalized.encode("utf-8"), code_hash.encode("utf-8"))
    except Exception:
        return False