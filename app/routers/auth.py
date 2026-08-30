import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_settings, get_supabase_admin, get_supabase_anon
from app.utils.otp import generate_otp_code, hash_otp, compare_otp, get_otp_expiry
from app.utils.mailer import send_otp_email

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger("datalume.auth")


def _send_otp_email_safe(to_email: str, code: str) -> None:
    """Envía el correo de OTP en segundo plano; los errores solo se registran,
    ya que en este punto la respuesta al cliente ya fue enviada."""
    try:
        send_otp_email(to_email, code)
    except Exception:
        logger.exception("No se pudo enviar el correo de verificación a %s", to_email)


# ---------------------------------------------------------
# Esquemas
# ---------------------------------------------------------
class RegisterInput(BaseModel):
    model_config = {"populate_by_name": True}

    full_name: str = Field(min_length=1, alias="fullName")
    email: EmailStr
    company: str | None = None
    password: str = Field(min_length=8)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class VerifyOtpInput(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4)


class ResendOtpInput(BaseModel):
    email: EmailStr


class RefreshInput(BaseModel):
    refresh_token: str = Field(min_length=1)


# ---------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------
@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterInput):
    supabase = get_supabase_admin()

    try:
        result = supabase.auth.admin.create_user(
            {
                "email": payload.email,
                "password": payload.password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": payload.full_name,
                    "company": payload.company,
                },
            }
        )
    except Exception as exc:
        message = str(exc)
        code = status.HTTP_409_CONFLICT if "already" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    return {
        "message": "Cuenta creada correctamente. Ya puedes iniciar sesión.",
        "userId": result.user.id,
    }


# ---------------------------------------------------------
# POST /auth/login (paso 1: correo + contraseña)
# ---------------------------------------------------------
@router.post("/login")
def login(payload: LoginInput, background_tasks: BackgroundTasks):
    settings = get_settings()
    supabase_anon = get_supabase_anon()
    supabase_admin = get_supabase_admin()

    try:
        auth_response = supabase_anon.auth.sign_in_with_password(
            {"email": payload.email, "password": payload.password}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos")

    if not auth_response or not auth_response.session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña incorrectos")

    user_id = auth_response.user.id
    access_token = auth_response.session.access_token
    refresh_token = auth_response.session.refresh_token

    # Invalida OTPs anteriores no consumidos
    supabase_admin.table("login_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).is_("consumed_at", "null").execute()

    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase_admin.table("login_otps").insert(
        {
            "user_id": user_id,
            "email": payload.email,
            "code_hash": code_hash,
            "max_attempts": settings.otp_max_attempts,
            "pending_access_token": access_token,
            "pending_refresh_token": refresh_token,
            "expires_at": get_otp_expiry().isoformat(),
        }
    ).execute()

    background_tasks.add_task(_send_otp_email_safe, payload.email, code)

    return {
        "message": "Código de verificación enviado a tu correo",
        "email": payload.email,
        "requiresOtp": True,
    }


# ---------------------------------------------------------
# POST /auth/verify-otp (paso 2)
# ---------------------------------------------------------
@router.post("/verify-otp")
def verify_otp(payload: VerifyOtpInput):
    supabase_admin = get_supabase_admin()

    result = (
        supabase_admin.table("login_otps")
        .select("*")
        .eq("email", payload.email)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    rows = result.data or []
    if not rows:
        raise HTTPException(
            status_code=400, detail="No hay un código pendiente para este correo. Inicia sesión de nuevo."
        )

    otp_row = rows[0]

    expires_at = datetime.fromisoformat(otp_row["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="El código ha expirado. Inicia sesión de nuevo.")

    if otp_row["attempts"] >= otp_row["max_attempts"]:
        raise HTTPException(status_code=429, detail="Se agotaron los intentos. Inicia sesión de nuevo.")

    if not compare_otp(payload.code, otp_row["code_hash"]):
        supabase_admin.table("login_otps").update({"attempts": otp_row["attempts"] + 1}).eq(
            "id", otp_row["id"]
        ).execute()
        raise HTTPException(status_code=401, detail="Código incorrecto")

    supabase_admin.table("login_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", otp_row["id"]).execute()

    return {
        "message": "Verificación exitosa",
        "session": {
            "access_token": otp_row["pending_access_token"],
            "refresh_token": otp_row["pending_refresh_token"],
        },
    }


# ---------------------------------------------------------
# POST /auth/resend-otp
# ---------------------------------------------------------
@router.post("/resend-otp")
def resend_otp(payload: ResendOtpInput, background_tasks: BackgroundTasks):
    supabase_admin = get_supabase_admin()

    result = (
        supabase_admin.table("login_otps")
        .select("*")
        .eq("email", payload.email)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=400, detail="No hay un inicio de sesión pendiente para este correo")

    otp_row = rows[0]
    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase_admin.table("login_otps").update(
        {
            "code_hash": code_hash,
            "attempts": 0,
            "expires_at": get_otp_expiry().isoformat(),
        }
    ).eq("id", otp_row["id"]).execute()

    background_tasks.add_task(_send_otp_email_safe, payload.email, code)

    return {"message": "Código reenviado"}


# ---------------------------------------------------------
# POST /auth/refresh — renueva el access token usando el refresh token
# ---------------------------------------------------------
@router.post("/refresh")
def refresh_session(payload: RefreshInput):
    supabase_anon = get_supabase_anon()

    try:
        auth_response = supabase_anon.auth.refresh_session(payload.refresh_token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida o expirada")

    if not auth_response or not auth_response.session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida o expirada")

    return {
        "session": {
            "access_token": auth_response.session.access_token,
            "refresh_token": auth_response.session.refresh_token,
        }
    }