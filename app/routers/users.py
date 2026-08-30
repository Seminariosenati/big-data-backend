import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_settings, get_supabase_admin
from app.utils.auth_dependency import require_auth, require_role
from app.utils.otp import compare_otp, generate_otp_code, get_otp_expiry, hash_otp
from app.utils.mailer import send_otp_email

router = APIRouter(prefix="/users", tags=["users"])

logger = logging.getLogger("datalume.users")

DEFAULT_PERMISSIONS = {
    "ventas": True,
    "ventas_resumen": True,
    "ventas_clientes": True,
    "ventas_comparacion": True,
    "cargar": False,
    "explorar": False,
    "reportes": True,
}


class CreateAnalystInput(BaseModel):
    model_config = {"populate_by_name": True}

    full_name: str = Field(min_length=1, alias="fullName")
    email: EmailStr
    password: str = Field(min_length=8)


class RequestCreateAdminInput(BaseModel):
    model_config = {"populate_by_name": True}

    full_name: str = Field(min_length=1, alias="fullName")
    email: EmailStr
    password: str = Field(min_length=8)


class VerifyCreateAdminInput(BaseModel):
    code: str = Field(min_length=4)


class UpdatePermissionsInput(BaseModel):
    ventas: bool
    ventas_resumen: bool
    ventas_clientes: bool
    ventas_comparacion: bool
    cargar: bool
    explorar: bool
    reportes: bool


# ---------------------------------------------------------
# GET /users  — el admin lista a sus analistas (con sus permisos)
# ---------------------------------------------------------
@router.get("")
def list_analysts(auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    profiles = (
        supabase.table("profiles")
        .select("id, full_name, phone, created_at")
        .eq("owner_id", admin_id)
        .eq("role", "analyst")
        .order("created_at", desc=False)
        .execute()
    )
    analyst_ids = [p["id"] for p in (profiles.data or [])]

    perms_by_id: dict[str, dict] = {}
    if analyst_ids:
        perms = (
            supabase.table("analyst_permissions")
            .select("*")
            .in_("user_id", analyst_ids)
            .execute()
        )
        perms_by_id = {row["user_id"]: row for row in (perms.data or [])}

    result = []
    for p in profiles.data or []:
        auth_user = supabase.auth.admin.get_user_by_id(p["id"])
        email = auth_user.user.email if auth_user and auth_user.user else None
        permissions = perms_by_id.get(p["id"], {**DEFAULT_PERMISSIONS, "user_id": p["id"]})
        result.append({
            "id": p["id"],
            "full_name": p["full_name"],
            "email": email,
            "phone": p["phone"],
            "created_at": p["created_at"],
            "permissions": permissions,
        })

    return result


# ---------------------------------------------------------
# POST /users  — el admin crea una cuenta de analista
# ---------------------------------------------------------
@router.post("", status_code=status.HTTP_201_CREATED)
def create_analyst(payload: CreateAnalystInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    try:
        result = supabase.auth.admin.create_user(
            {
                "email": payload.email,
                "password": payload.password,
                "email_confirm": True,
                "user_metadata": {"full_name": payload.full_name},
            }
        )
    except Exception as exc:
        message = str(exc)
        code = status.HTTP_409_CONFLICT if "already" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    new_user_id = result.user.id

    # El trigger on_auth_user_created ya insertó una fila en profiles con
    # role='admin' por defecto; la corregimos a analyst + owner_id.
    supabase.table("profiles").update(
        {"role": "analyst", "owner_id": admin_id, "full_name": payload.full_name}
    ).eq("id", new_user_id).execute()

    supabase.table("analyst_permissions").insert(
        {"user_id": new_user_id, "created_by": admin_id, **DEFAULT_PERMISSIONS}
    ).execute()

    return {
        "message": "Cuenta de analista creada correctamente.",
        "id": new_user_id,
        "email": payload.email,
        "permissions": DEFAULT_PERMISSIONS,
    }


def _send_admin_otp_email_safe(to_email: str, code: str) -> None:
    """Envía el correo de verificación en segundo plano; los errores solo se
    registran, porque en este punto la respuesta al cliente ya fue enviada."""
    try:
        send_otp_email(to_email, code)
    except Exception:
        logger.exception("No se pudo enviar el correo de verificación de admin a %s", to_email)


# ---------------------------------------------------------
# POST /users/admins/request — un admin pide crear OTRA cuenta de admin.
# Como es un rol fuerte (acceso total), antes de crearla se le manda un
# código de verificación a SU PROPIO correo (el de quien hace la acción),
# a modo de confirmación extra. La cuenta recién se crea al verificar el
# código en /users/admins/verify.
# ---------------------------------------------------------
@router.post("/admins/request")
def request_create_admin(
    payload: RequestCreateAdminInput, background_tasks: BackgroundTasks, auth=Depends(require_role("admin"))
):
    supabase = get_supabase_admin()
    settings = get_settings()
    acting_admin = auth["user"]

    if not acting_admin.email:
        raise HTTPException(status_code=400, detail="Tu cuenta no tiene un correo registrado para enviar el código")

    # Invalida cualquier solicitud pendiente anterior de este mismo admin
    supabase.table("admin_creation_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("requested_by", acting_admin.id).is_("consumed_at", "null").execute()

    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase.table("admin_creation_otps").insert(
        {
            "requested_by": acting_admin.id,
            "email": acting_admin.email,
            "code_hash": code_hash,
            "max_attempts": settings.otp_max_attempts,
            "pending_full_name": payload.full_name,
            "pending_email": payload.email,
            "pending_password": payload.password,
            "expires_at": get_otp_expiry().isoformat(),
        }
    ).execute()

    background_tasks.add_task(_send_admin_otp_email_safe, acting_admin.email, code)

    return {
        "message": "Te enviamos un código de verificación a tu correo para confirmar la creación de esta cuenta de administrador.",
        "email": acting_admin.email,
        "requiresOtp": True,
    }


# ---------------------------------------------------------
# POST /users/admins/verify — valida el código y recién ahí crea la
# cuenta de administrador con los datos que quedaron pendientes.
# ---------------------------------------------------------
@router.post("/admins/verify", status_code=status.HTTP_201_CREATED)
def verify_create_admin(payload: VerifyCreateAdminInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    result = (
        supabase.table("admin_creation_otps")
        .select("*")
        .eq("requested_by", admin_id)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(
            status_code=400, detail="No hay una solicitud pendiente. Vuelve a completar el formulario."
        )

    otp_row = rows[0]

    expires_at = datetime.fromisoformat(otp_row["expires_at"].replace("Z", "+00:00"))
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="El código ha expirado. Vuelve a completar el formulario.")

    if otp_row["attempts"] >= otp_row["max_attempts"]:
        raise HTTPException(status_code=429, detail="Se agotaron los intentos. Vuelve a completar el formulario.")

    if not compare_otp(payload.code, otp_row["code_hash"]):
        supabase.table("admin_creation_otps").update({"attempts": otp_row["attempts"] + 1}).eq(
            "id", otp_row["id"]
        ).execute()
        raise HTTPException(status_code=401, detail="Código incorrecto")

    supabase.table("admin_creation_otps").update(
        {"consumed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", otp_row["id"]).execute()

    try:
        result = supabase.auth.admin.create_user(
            {
                "email": otp_row["pending_email"],
                "password": otp_row["pending_password"],
                "email_confirm": True,
                "user_metadata": {"full_name": otp_row["pending_full_name"]},
            }
        )
    except Exception as exc:
        message = str(exc)
        code = status.HTTP_409_CONFLICT if "already" in message.lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=code, detail=message)

    new_user_id = result.user.id

    # El trigger on_auth_user_created ya crea el perfil con role='admin' por
    # defecto y owner_id=None (un admin siempre es dueño de sí mismo); solo
    # nos aseguramos de que el nombre completo quede guardado.
    supabase.table("profiles").update({"full_name": otp_row["pending_full_name"]}).eq("id", new_user_id).execute()

    return {
        "message": "Cuenta de administrador creada correctamente.",
        "id": new_user_id,
        "email": otp_row["pending_email"],
    }


# ---------------------------------------------------------
# POST /users/admins/resend — reenvía el código al correo del admin que
# está creando la cuenta (no al correo de la cuenta nueva).
# ---------------------------------------------------------
@router.post("/admins/resend")
def resend_create_admin_otp(background_tasks: BackgroundTasks, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    result = (
        supabase.table("admin_creation_otps")
        .select("*")
        .eq("requested_by", admin_id)
        .is_("consumed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        raise HTTPException(status_code=400, detail="No hay una solicitud pendiente para reenviar el código")

    otp_row = rows[0]
    code = generate_otp_code()
    code_hash = hash_otp(code)

    supabase.table("admin_creation_otps").update(
        {"code_hash": code_hash, "attempts": 0, "expires_at": get_otp_expiry().isoformat()}
    ).eq("id", otp_row["id"]).execute()

    background_tasks.add_task(_send_admin_otp_email_safe, otp_row["email"], code)

    return {"message": "Código reenviado"}


# ---------------------------------------------------------
# PUT /users/{analyst_id}/permissions — el admin edita los permisos
# de un analista específico (solo si le pertenece)
# ---------------------------------------------------------
@router.put("/{analyst_id}/permissions")
def update_analyst_permissions(
    analyst_id: str, payload: UpdatePermissionsInput, auth=Depends(require_role("admin"))
):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    owned = (
        supabase.table("profiles")
        .select("id")
        .eq("id", analyst_id)
        .eq("owner_id", admin_id)
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(status_code=404, detail="Ese analista no pertenece a tu cuenta")

    data = {"user_id": analyst_id, "created_by": admin_id, **payload.model_dump()}
    result = (
        supabase.table("analyst_permissions")
        .upsert(data, on_conflict="user_id")
        .execute()
    )
    return result.data[0] if result.data else data


class UpdateDatasetAccessInput(BaseModel):
    dataset_ids: list[str]


# ---------------------------------------------------------
# GET /users/{analyst_id}/datasets — datasets del admin + cuáles
# tiene habilitados este analista
# ---------------------------------------------------------
@router.get("/{analyst_id}/datasets")
def list_analyst_dataset_access(analyst_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    owned = (
        supabase.table("profiles")
        .select("id")
        .eq("id", analyst_id)
        .eq("owner_id", admin_id)
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(status_code=404, detail="Ese analista no pertenece a tu cuenta")

    datasets = (
        supabase.table("datasets")
        .select("id, file_name, created_at")
        .eq("user_id", admin_id)
        .order("created_at", desc=True)
        .execute()
    )

    access = (
        supabase.table("analyst_dataset_access")
        .select("dataset_id")
        .eq("analyst_id", analyst_id)
        .execute()
    )
    allowed_ids = {row["dataset_id"] for row in (access.data or [])}

    return [
        {"id": d["id"], "file_name": d["file_name"], "allowed": d["id"] in allowed_ids}
        for d in (datasets.data or [])
    ]


# ---------------------------------------------------------
# PUT /users/{analyst_id}/datasets — reemplaza el set completo de
# datasets habilitados para ese analista
# ---------------------------------------------------------
@router.put("/{analyst_id}/datasets")
def update_analyst_dataset_access(
    analyst_id: str, payload: UpdateDatasetAccessInput, auth=Depends(require_role("admin"))
):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    owned = (
        supabase.table("profiles")
        .select("id")
        .eq("id", analyst_id)
        .eq("owner_id", admin_id)
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(status_code=404, detail="Ese analista no pertenece a tu cuenta")

    # Solo se permiten datasets que de verdad son del admin (nunca de otro).
    valid_ids: list[str] = []
    if payload.dataset_ids:
        valid = (
            supabase.table("datasets")
            .select("id")
            .eq("user_id", admin_id)
            .in_("id", payload.dataset_ids)
            .execute()
        )
        valid_ids = [d["id"] for d in (valid.data or [])]

    supabase.table("analyst_dataset_access").delete().eq("analyst_id", analyst_id).execute()
    if valid_ids:
        supabase.table("analyst_dataset_access").insert(
            [{"analyst_id": analyst_id, "dataset_id": did} for did in valid_ids]
        ).execute()

    return {"dataset_ids": valid_ids}


# ---------------------------------------------------------
# DELETE /users/{analyst_id} — el admin elimina una cuenta de analista
# ---------------------------------------------------------
@router.delete("/{analyst_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_analyst(analyst_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    admin_id = auth["user"].id

    owned = (
        supabase.table("profiles")
        .select("id")
        .eq("id", analyst_id)
        .eq("owner_id", admin_id)
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(status_code=404, detail="Ese analista no pertenece a tu cuenta")

    supabase.auth.admin.delete_user(analyst_id)
    return None