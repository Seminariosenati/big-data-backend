from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_auth, require_role

router = APIRouter(prefix="/users", tags=["users"])

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