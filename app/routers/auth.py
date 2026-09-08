import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_settings, get_supabase_admin, get_supabase_anon
from app.utils.otp import generate_otp_code, hash_otp, compare_otp, get_otp_expiry
from app.utils.mailer import send_otp_email
from app.utils.auth_dependency import require_auth
from app.utils.totp import (
    generate_totp_secret,
    get_provisioning_uri,
    generate_qr_code_data_uri,
    verify_totp_code,
    generate_recovery_codes,
    hash_recovery_code,
    compare_recovery_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])

logger = logging.getLogger("datalume.auth")


def _send_otp_email_safe(to_email: str, code: str) -> None:
    """Envía el correo de OTP en segundo plano; los errores solo se registran,
    ya que en este punto la respuesta al cliente ya fue enviada."""
    try:
        send_otp_email(to_email, code)
    except Exception:
        logger.exception("No se pudo enviar el correo de verificación a %s", to_email)


def _verify_totp_login_step(supabase_admin, user_id: str, code: str) -> bool:
    """Valida el código del paso 2 del login cuando el usuario tiene TOTP
    activo: primero contra el código de 6 dígitos de su app autenticadora,
    y si no coincide, contra sus códigos de recuperación sin usar."""
    profile = supabase_admin.table("profiles").select("totp_secret").eq("id", user_id).limit(1).execute()
    secret = profile.data[0].get("totp_secret") if profile.data else None
    if secret and verify_totp_code(secret, code):
        return True

    candidates = (
        supabase_admin.table("totp_recovery_codes")
        .select("id, code_hash")
        .eq("user_id", user_id)
        .is_("used_at", "null")
        .execute()
    )
    for row in candidates.data or []:
        if compare_recovery_code(code, row["code_hash"]):
            supabase_admin.table("totp_recovery_codes").update(
                {"used_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", row["id"]).execute()
            return True
    return False


# ---------------------------------------------------------
# Esquemas
# ---------------------------------------------------------
class VerifyOtpInput(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4)


class ResendOtpInput(BaseModel):
    email: EmailStr


class RefreshInput(BaseModel):
    refresh_token: str = Field(min_length=1)


class TwoFaConfirmInput(BaseModel):
    code: str = Field(min_length=4)


class TwoFaDisableInput(BaseModel):
    code: str = Field(min_length=4)


# ---------------------------------------------------------
# NOTA: el login independiente de Datalume (/auth/datalume/login) fue
# retirado. El Portal es ahora el único punto de entrada: su sesión
# (misma cookie/JWT de Supabase) sirve directamente para llamar a la
# API de Datalume, y el rol guardado en profiles decide qué panel ve
# cada quien. Los helpers _is_email_whitelisted/_ensure_auth_user de
# abajo siguen en uso por /auth/portal/login.
# ---------------------------------------------------------


# ---------------------------------------------------------
# POST /auth/verify-otp (paso 2, compartido por Portal)
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

    method = otp_row.get("method") or "email"
    if method == "totp":
        code_valid = _verify_totp_login_step(supabase_admin, otp_row["user_id"], payload.code)
    else:
        code_valid = bool(otp_row["code_hash"]) and compare_otp(payload.code, otp_row["code_hash"])

    if not code_valid:
        supabase_admin.table("login_otps").update({"attempts": otp_row["attempts"] + 1}).eq(
            "id", otp_row["id"]
        ).execute()
        detail = "Código incorrecto" if method == "email" else "Código incorrecto o vencido"
        raise HTTPException(status_code=401, detail=detail)

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
# PORTAL: login solo con correo (sin contraseña). Solo correos en
# whitelist (pending_signups aprobados, profiles existentes, o
# invitaciones válidas) pueden solicitar acceso. El segundo paso
# depende de la cuenta:
#   - Si activó una app autenticadora (2fa/setup): se le pide el
#     código de 6 dígitos de su app (o un código de recuperación).
#   - Si no, el código de un solo uso se envía al ADMIN_EMAIL, quien
#     se lo comparte para completar el ingreso.
# ---------------------------------------------------------

class PortalLoginInput(BaseModel):
    email: EmailStr
    # Método elegido por el usuario en el paso 1 del login, SOLO relevante
    # cuando la cuenta tiene la app autenticadora activa (totp_enabled=True):
    # le permite optar por recibir igual el código OTP por correo al admin
    # en vez de usar su app. Si la cuenta no tiene TOTP activo, este campo
    # se ignora (solo existe el método por correo). Valores: "totp" | "email".
    method: str | None = None


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
    is_new_user = existing_id is None

    if existing_id:
        supabase_admin.auth.admin.update_user_by_id(
            existing_id,
            {"password": temp_password, "email_confirm": True},
        )
        user_id = existing_id
    else:
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
    # vienen de ahí. Solo el ADMIN_EMAIL configurado puede ser 'admin' del
    # portal; cualquier otra cuenta nueva sin invitación (ej. entró por
    # pending_signups) cae en 'analyst' por default — nunca en 'admin'.
    # Si la cuenta ya existía y no tiene invitaciones pendientes, no se toca
    # su perfil/acceso: ya se creó antes.
    settings = get_settings()
    is_admin_email = bool(settings.admin_email) and email_l == settings.admin_email.lower().strip()
    if is_new_user:
        role = "admin" if is_admin_email else "analyst"
    else:
        role = None
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

    if role is None:
        # Cuenta existente sin invitaciones pendientes: nada nuevo que crear.
        return user_id, temp_password

    # El "entorno de datos" (profiles.owner_id) es lo que de verdad controla
    # qué datasets puede ver el analista dentro del proyecto (ej. Datalume).
    # Se toma del primer proyecto de la invitación; si ese proyecto todavía
    # no tiene un entorno vinculado (projects.env_owner_id), el analista
    # queda sin datasets hasta que se configure, pero sí puede entrar.
    owner_id = None
    if project_ids:
        try:
            proj = (
                supabase_admin.table("projects")
                .select("env_owner_id")
                .eq("id", project_ids[0])
                .limit(1)
                .execute()
            )
            if proj.data:
                owner_id = proj.data[0].get("env_owner_id")
        except Exception:
            logger.exception("No se pudo leer el entorno de datos del proyecto %s", project_ids[0])

    # A partir de aquí puede haber trabajo pendiente (perfil y/o acceso a
    # proyectos). Si algo de esto falla, la invitación NO se marca como
    # usada: así queda "Pendiente" en el panel y se puede reintentar, en vez
    # de quedar "Usada" para siempre sin una cuenta funcional detrás.
    setup_failed = False

    try:
        # El trigger on_auth_user_created de la base ya garantiza que esta
        # fila existe (con role='analyst' por default); acá la dejamos con
        # el rol y el entorno de datos que le corresponden según su
        # invitación, en vez de solo insertar si faltara.
        profile_update = {"full_name": email_l.split("@")[0], "role": role}
        if owner_id:
            profile_update["owner_id"] = owner_id
        supabase_admin.table("profiles").update(profile_update).eq("id", user_id).execute()
    except Exception:
        logger.exception("No se pudo actualizar profile para %s", email_l)
        setup_failed = True

    for project_id in project_ids:
        try:
            supabase_admin.table("project_access").insert(
                {"project_id": project_id, "user_id": user_id, "role": role}
            ).execute()
        except Exception:
            logger.exception("No se pudo dar acceso al proyecto %s para %s", project_id, email_l)
            setup_failed = True

    if invitations and not setup_failed:
        try:
            supabase_admin.table("access_invitations").update({"used": True}).eq(
                "email", email_l
            ).eq("used", False).execute()
        except Exception:
            logger.exception("No se pudo marcar como usada la invitación de %s", email_l)
            setup_failed = True

    if setup_failed:
        raise RuntimeError(
            f"La cuenta de {email_l} se preparó parcialmente pero falló crear su perfil o "
            "su acceso a proyecto. Revisa los logs del backend antes de reintentar."
        )

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

    # Si la cuenta activó una app autenticadora (Google Authenticator, Authy,
    # etc. — configurable desde /auth/2fa/setup), tiene dos métodos posibles
    # para este segundo paso: su app autenticadora, o el OTP de siempre por
    # correo al admin. Si el cliente todavía no indicó cuál prefiere,
    # devolvemos las opciones para que el usuario elija en el login (sin
    # generar todavía ningún código ni fila de login_otps).
    profile_result = (
        supabase_admin.table("profiles").select("totp_enabled").eq("id", user_id).limit(1).execute()
    )
    totp_enabled = bool(profile_result.data and profile_result.data[0].get("totp_enabled"))

    chosen_method = (payload.method or "").strip().lower() or None
    if chosen_method not in (None, "totp", "email"):
        chosen_method = None

    if totp_enabled and chosen_method is None:
        return {
            "message": "Elige cómo quieres verificar tu identidad",
            "email": email_l,
            "requiresOtp": True,
            "requiresMethodSelection": True,
            "availableMethods": ["totp", "email"],
        }

    supabase_admin.table("login_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("user_id", user_id).is_("consumed_at", "null").execute()

    if totp_enabled and chosen_method == "totp":
        supabase_admin.table("login_otps").insert(
            {
                "user_id": user_id,
                "email": email_l,
                "code_hash": None,
                "method": "totp",
                "max_attempts": settings.otp_max_attempts,
                "pending_access_token": access_token,
                "pending_refresh_token": refresh_token,
                "expires_at": get_otp_expiry().isoformat(),
            }
        ).execute()

        return {
            "message": "Ingresa el código de tu app autenticadora",
            "email": email_l,
            "requiresOtp": True,
            "method": "totp",
            "otpDestination": "self",
        }

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

    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase_admin.table("login_otps").insert(
        {
            "user_id": user_id,
            "email": email_l,
            "code_hash": code_hash,
            "method": "email",
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
        "method": "email",
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
    if (otp_row.get("method") or "email") == "totp":
        raise HTTPException(
            status_code=400,
            detail="Esta cuenta usa una app autenticadora; no hay código para reenviar.",
        )

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


# ---------------------------------------------------------
# 2FA con app autenticadora (Google Authenticator, Authy, etc.)
# Vive bajo /auth/2fa/* y siempre requiere sesión activa: es el
# propio usuario configurando su cuenta, no parte del login.
# ---------------------------------------------------------


@router.get("/2fa/status")
def get_2fa_status(auth: dict = Depends(require_auth)):
    supabase_admin = get_supabase_admin()
    profile = (
        supabase_admin.table("profiles")
        .select("totp_enabled, totp_confirmed_at")
        .eq("id", auth["user"].id)
        .limit(1)
        .execute()
    )
    data = profile.data[0] if profile.data else {}
    return {
        "enabled": bool(data.get("totp_enabled")),
        "confirmedAt": data.get("totp_confirmed_at"),
    }


@router.post("/2fa/setup")
def setup_2fa(auth: dict = Depends(require_auth)):
    """Genera un secreto nuevo (pendiente de confirmar) y el QR para escanear."""
    supabase_admin = get_supabase_admin()
    user = auth["user"]

    existing = supabase_admin.table("profiles").select("totp_enabled").eq("id", user.id).limit(1).execute()
    if existing.data and existing.data[0].get("totp_enabled"):
        raise HTTPException(
            status_code=400,
            detail="La autenticación de 2 factores ya está activa. Desactívala antes de generar un nuevo código.",
        )

    secret = generate_totp_secret()
    supabase_admin.table("profiles").update(
        {"totp_secret": secret, "totp_enabled": False, "totp_confirmed_at": None}
    ).eq("id", user.id).execute()

    otpauth_url = get_provisioning_uri(secret, user.email)

    return {
        "secret": secret,
        "otpauthUrl": otpauth_url,
        "qrCode": generate_qr_code_data_uri(otpauth_url),
    }


@router.post("/2fa/confirm")
def confirm_2fa(payload: TwoFaConfirmInput, auth: dict = Depends(require_auth)):
    """Confirma el código mostrado por la app autenticadora y activa el 2FA."""
    supabase_admin = get_supabase_admin()
    user = auth["user"]

    profile = supabase_admin.table("profiles").select("totp_secret").eq("id", user.id).limit(1).execute()
    secret = profile.data[0].get("totp_secret") if profile.data else None
    if not secret:
        raise HTTPException(status_code=400, detail="Primero genera un código QR desde /auth/2fa/setup.")

    if not verify_totp_code(secret, payload.code):
        raise HTTPException(
            status_code=401,
            detail="El código no es válido. Revisa la hora de tu teléfono e inténtalo de nuevo.",
        )

    supabase_admin.table("profiles").update(
        {"totp_enabled": True, "totp_confirmed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", user.id).execute()

    # Códigos de recuperación nuevos cada vez que se (re)activa el 2FA
    supabase_admin.table("totp_recovery_codes").delete().eq("user_id", user.id).execute()
    codes = generate_recovery_codes()
    supabase_admin.table("totp_recovery_codes").insert(
        [{"user_id": user.id, "code_hash": hash_recovery_code(c)} for c in codes]
    ).execute()

    return {
        "message": "Autenticación de 2 factores activada",
        "recoveryCodes": codes,
    }


@router.post("/2fa/disable")
def disable_2fa(payload: TwoFaDisableInput, auth: dict = Depends(require_auth)):
    """Desactiva el 2FA. Como ya no hay contraseñas, se pide de nuevo el
    código de la app (o uno de recuperación) para evitar que una sesión
    abierta en un dispositivo ajeno lo desactive sin más."""
    supabase_admin = get_supabase_admin()
    user = auth["user"]

    profile = supabase_admin.table("profiles").select("totp_secret").eq("id", user.id).limit(1).execute()
    secret = profile.data[0].get("totp_secret") if profile.data else None

    code_valid = bool(secret) and verify_totp_code(secret, payload.code)
    if not code_valid:
        candidates = (
            supabase_admin.table("totp_recovery_codes")
            .select("id, code_hash")
            .eq("user_id", user.id)
            .is_("used_at", "null")
            .execute()
        )
        for row in candidates.data or []:
            if compare_recovery_code(payload.code, row["code_hash"]):
                code_valid = True
                break

    if not code_valid:
        raise HTTPException(status_code=401, detail="Código incorrecto")

    supabase_admin.table("profiles").update(
        {"totp_enabled": False, "totp_secret": None, "totp_confirmed_at": None}
    ).eq("id", user.id).execute()
    supabase_admin.table("totp_recovery_codes").delete().eq("user_id", user.id).execute()

    return {"message": "Autenticación de 2 factores desactivada"}


@router.post("/2fa/recovery-codes/regenerate")
def regenerate_recovery_codes(auth: dict = Depends(require_auth)):
    """Invalida los códigos de recuperación anteriores y genera un set nuevo."""
    supabase_admin = get_supabase_admin()
    user = auth["user"]

    profile = supabase_admin.table("profiles").select("totp_enabled").eq("id", user.id).limit(1).execute()
    if not profile.data or not profile.data[0].get("totp_enabled"):
        raise HTTPException(status_code=400, detail="Activa primero la autenticación de 2 factores.")

    supabase_admin.table("totp_recovery_codes").delete().eq("user_id", user.id).execute()
    codes = generate_recovery_codes()
    supabase_admin.table("totp_recovery_codes").insert(
        [{"user_id": user.id, "code_hash": hash_recovery_code(c)} for c in codes]
    ).execute()

    return {"recoveryCodes": codes}