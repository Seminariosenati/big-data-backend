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

    if not settings.otp_enabled:
        return {
            "message": "Sesión iniciada (OTP desactivado)",
            "email": payload.email,
            "requiresOtp": False,
            "session": {"access_token": access_token, "refresh_token": refresh_token},
        }

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


# ---------------------------------------------------------
# PORTAL: login solo con correo (sin contraseña)
# El OTP se envía al ADMIN_EMAIL. Solo correos en whitelist
# (pending_signups aprobados, profiles existentes, o
# invitaciones válidas) pueden solicitar acceso.
# ---------------------------------------------------------

class PortalLoginInput(BaseModel):
    email: EmailStr


def _find_auth_user_by_email(supabase_admin, email: str):
    """Busca un usuario de auth por email. Devuelve el objeto user o None."""
    email_l = email.lower().strip()
    try:
        getter = getattr(supabase_admin.auth.admin, "get_user_by_email", None)
        if callable(getter):
            res = getter(email_l)
            user = getattr(res, "user", res)
            if user and getattr(user, "email", None):
                return user
    except Exception:
        pass

    try:
        page = supabase_admin.auth.admin.list_users()
        iterable = page.users if hasattr(page, "users") else (page or [])
        for u in iterable:
            if getattr(u, "email", None) and u.email.lower() == email_l:
                return u
    except Exception:
        logger.exception("list_users falló buscando %s", email_l)
    return None


def _is_email_whitelisted(supabase_admin, email: str) -> tuple[bool, str | None]:
    """Devuelve (allowed, user_id_si_existe)."""
    email_l = email.lower().strip()
    settings = get_settings()
    if settings.admin_email and email_l == settings.admin_email.lower().strip():
        return True, None

    matched = _find_auth_user_by_email(supabase_admin, email_l)
    if matched is not None:
        banned_until = getattr(matched, "banned_until", None)
        if banned_until:
            try:
                bu = datetime.fromisoformat(str(banned_until).replace("Z", "+00:00"))
                if bu > datetime.now(timezone.utc):
                    return False, None
            except Exception:
                pass
        return True, str(matched.id)

    try:
        ps = (
            supabase_admin.table("pending_signups")
            .select("id, status")
            .eq("email", email_l)
            .limit(1)
            .execute()
        )
        if ps.data:
            status_val = (ps.data[0].get("status") or "pending").lower()
            if status_val in ("pending", "approved", "invited"):
                return True, None
    except Exception:
        logger.exception("Error consultando pending_signups")

    try:
        inv = (
            supabase_admin.table("access_invitations")
            .select("id, expires_at, used")
            .eq("email", email_l)
            .eq("used", False)
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        now = datetime.now(timezone.utc)
        for row in inv.data or []:
            exp = row.get("expires_at")
            if exp:
                try:
                    exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                    if exp_dt < now:
                        continue
                except Exception:
                    pass
            return True, None
    except Exception:
        logger.exception("Error consultando access_invitations")

    return False, None


def _ensure_auth_user(supabase_admin, email: str) -> tuple[str, str]:
    """Asegura que exista un usuario en auth.users. Devuelve (user_id, temp_password)."""
    import secrets
    import string

    email_l = email.lower().strip()
    temp_password = "Tmp!" + "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(24))

    existing = _find_auth_user_by_email(supabase_admin, email_l)
    existing_id = str(existing.id) if existing is not None else None

    if existing_id:
        supabase_admin.auth.admin.update_user_by_id(
            existing_id,
            {"password": temp_password, "email_confirm": True},
        )
        return existing_id, temp_password

    result = supabase_admin.auth.admin.create_user(
        {
            "email": email_l,
            "password": temp_password,
            "email_confirm": True,
            "user_metadata": {"source": "portal_invite"},
        }
    )
    user_id = result.user.id

    # Revisa si este correo tiene invitaciones pendientes (creadas desde el
    # panel de admin). Si las hay, el rol del perfil y el acceso a proyectos
    # vienen de ahí. Si no hay ninguna (ej. el ADMIN_EMAIL entrando por
    # primera vez), se asume 'admin' para no romper el flujo actual.
    role = "admin"
    project_ids: list[str] = []
    invitations: list[dict] = []
    try:
        inv = (
            supabase_admin.table("access_invitations")
            .select("id, project_id, type")
            .eq("email", email_l)
            .eq("used", False)
            .execute()
        )
        invitations = inv.data or []
        if invitations:
            role = invitations[0].get("type") or "analyst"
            project_ids = [row["project_id"] for row in invitations if row.get("project_id")]
    except Exception:
        logger.exception("No se pudieron leer invitaciones para %s", email_l)
        invitations = []

    try:
        existing_profile = (
            supabase_admin.table("profiles").select("id").eq("id", user_id).limit(1).execute()
        )
        if not existing_profile.data:
            supabase_admin.table("profiles").insert(
                {
                    "id": user_id,
                    "full_name": email_l.split("@")[0],
                    "role": role,
                }
            ).execute()
    except Exception:
        logger.exception("No se pudo crear profile para %s", email_l)

    for project_id in project_ids:
        try:
            supabase_admin.table("project_access").insert(
                {"project_id": project_id, "user_id": user_id, "role": role}
            ).execute()
        except Exception:
            logger.exception("No se pudo dar acceso al proyecto %s para %s", project_id, email_l)

    if invitations:
        try:
            supabase_admin.table("access_invitations").update({"used": True}).eq(
                "email", email_l
            ).eq("used", False).execute()
        except Exception:
            pass

    try:
        supabase_admin.table("pending_signups").update({"status": "approved"}).eq(
            "email", email_l
        ).execute()
    except Exception:
        pass

    return user_id, temp_password


@router.post("/portal/login")
def portal_login(payload: PortalLoginInput, background_tasks: BackgroundTasks):
    settings = get_settings()
    supabase_admin = get_supabase_admin()
    supabase_anon = get_supabase_anon()

    if not settings.admin_email:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_EMAIL no está configurado en el servidor",
        )

    email_l = payload.email.lower().strip()
    allowed, _ = _is_email_whitelisted(supabase_admin, email_l)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Este correo no tiene acceso. Solicita una invitación al administrador.",
        )

    try:
        user_id, temp_password = _ensure_auth_user(supabase_admin, email_l)
    except Exception as exc:
        logger.exception("Error asegurando usuario portal")
        raise HTTPException(status_code=400, detail=f"No se pudo preparar la cuenta: {exc}")

    try:
        auth_response = supabase_anon.auth.sign_in_with_password(
            {"email": email_l, "password": temp_password}
        )
    except Exception:
        raise HTTPException(status_code=401, detail="No se pudo iniciar sesión. Contacta al administrador.")

    if not auth_response or not auth_response.session:
        raise HTTPException(status_code=401, detail="No se pudo iniciar sesión. Contacta al administrador.")

    access_token = auth_response.session.access_token
    refresh_token = auth_response.session.refresh_token

    if not settings.otp_enabled:
        # Modo local/desarrollo: se salta el OTP para no gastar envíos de correo.
        return {
            "message": "Sesión iniciada (OTP desactivado)",
            "email": email_l,
            "requiresOtp": False,
            "otpDestination": "admin",
            "session": {
                "access_token": access_token,
                "refresh_token": refresh_token,
            },
        }

    supabase_admin.table("login_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).is_("consumed_at", "null").execute()

    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase_admin.table("login_otps").insert(
        {
            "user_id": user_id,
            "email": email_l,
            "code_hash": code_hash,
            "max_attempts": settings.otp_max_attempts,
            "pending_access_token": access_token,
            "pending_refresh_token": refresh_token,
            "expires_at": get_otp_expiry().isoformat(),
        }
    ).execute()

    background_tasks.add_task(_send_otp_email_safe, settings.admin_email, code)

    return {
        "message": "Código de verificación enviado al administrador",
        "email": email_l,
        "requiresOtp": True,
        "otpDestination": "admin",
    }


@router.post("/portal/verify-otp")
def portal_verify_otp(payload: VerifyOtpInput):
    return verify_otp(payload)


@router.post("/portal/resend-otp")
def portal_resend_otp(payload: ResendOtpInput, background_tasks: BackgroundTasks):
    settings = get_settings()
    supabase_admin = get_supabase_admin()

    if not settings.admin_email:
        raise HTTPException(status_code=500, detail="ADMIN_EMAIL no configurado")

    result = (
        supabase_admin.table("login_otps")
        .select("*")
        .eq("email", payload.email.lower().strip())
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

    background_tasks.add_task(_send_otp_email_safe, settings.admin_email, code)
    return {"message": "Código reenviado al administrador"}