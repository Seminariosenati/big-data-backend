import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_settings, get_supabase_admin
from app.utils.auth_dependency import require_role
from app.utils.mailer import send_invitation_email

router = APIRouter(prefix="/admin", tags=["admin"])

logger = logging.getLogger("datalume.admin")


# ---------------------------------------------------------
# POST /admin/invitations — crea invitación(es) a uno o más
# proyectos para un correo, y envía el correo. Siempre entra
# como 'analyst': no se puede invitar a nadie como admin,
# el único admin es el ADMIN_EMAIL fijo.
# ---------------------------------------------------------
class InvitationInput(BaseModel):
    email: EmailStr
    project_ids: list[str] = Field(default_factory=list, min_length=1)
    expires_days: int = Field(default=7, ge=1, le=90)


@router.post("/invitations", status_code=201)
def create_invitation(payload: InvitationInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    settings = get_settings()
    email_l = payload.email.lower().strip()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.expires_days)).isoformat()

    projects = (
        supabase.table("projects")
        .select("id, name")
        .in_("id", payload.project_ids)
        .execute()
    ).data or []
    if len(projects) != len(payload.project_ids):
        raise HTTPException(status_code=400, detail="Alguno de los project_ids no existe")

    created = []
    for project in projects:
        row = {
            "project_id": project["id"],
            "email": email_l,
            "token": secrets.token_urlsafe(32),
            "type": "analyst",
            "expires_at": expires_at,
            "used": False,
        }
        result = supabase.table("access_invitations").insert(row).execute()
        if result.data:
            created.append(result.data[0])

    project_names = ", ".join(p["name"] for p in projects)
    try:
        send_invitation_email(email_l, project_names, settings.frontend_url)
    except Exception:
        logger.exception("No se pudo enviar el correo de invitación a %s", email_l)

    return created


# ---------------------------------------------------------
# GET /admin/invitations — lista todas (pendientes y usadas)
# ---------------------------------------------------------
@router.get("/invitations")
def list_invitations(auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    result = (
        supabase.table("access_invitations")
        .select("id, email, project_id, type, expires_at, used, created_at, projects(name)")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------
# DELETE /admin/invitations/{id} — revoca una invitación
# ---------------------------------------------------------
@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    result = supabase.table("access_invitations").delete().eq("id", invitation_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Invitación no encontrada")


# ---------------------------------------------------------
# GET /admin/users — lista usuarios con su rol y proyectos
# ---------------------------------------------------------
@router.get("/users")
def list_users(auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    profiles = (supabase.table("profiles").select("*").execute()).data or []
    access_rows = (supabase.table("project_access").select("user_id, project_id, role").execute()).data or []
    projects = {p["id"]: p["name"] for p in (supabase.table("projects").select("id, name").execute()).data or []}

    access_by_user: dict[str, list[dict]] = {}
    for row in access_rows:
        access_by_user.setdefault(row["user_id"], []).append(
            {"project_id": row["project_id"], "project_name": projects.get(row["project_id"]), "role": row["role"]}
        )

    try:
        page = supabase.auth.admin.list_users()
        auth_users = page.users if hasattr(page, "users") else (page or [])
        emails = {str(u.id): u.email for u in auth_users}
    except Exception:
        logger.exception("No se pudo listar usuarios de auth")
        emails = {}

    return [
        {
            **profile,
            "email": emails.get(profile["id"]),
            "project_access": access_by_user.get(profile["id"], []),
        }
        for profile in profiles
    ]


# ---------------------------------------------------------
# PATCH /admin/users/{id} — cambia rol global (solo puede
# bajar a 'analyst', nunca promover a admin) y/o el acceso
# a proyectos con su rol específico por proyecto.
# ---------------------------------------------------------
class ProjectRoleInput(BaseModel):
    project_id: str
    role: str = Field(pattern="^(admin|analyst)$")


class UserUpdateInput(BaseModel):
    role: str | None = Field(default=None, pattern="^analyst$")
    full_name: str | None = Field(default=None, max_length=200)
    project_access: list[ProjectRoleInput] | None = None  # si viene, reemplaza el acceso completo


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserUpdateInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    if payload.role is not None:
        result = supabase.table("profiles").update({"role": payload.role}).eq("id", user_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

    if payload.full_name is not None:
        cleaned = payload.full_name.strip()
        result = (
            supabase.table("profiles")
            .update({"full_name": cleaned or None})
            .eq("id", user_id)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

    if payload.project_access is not None:
        supabase.table("project_access").delete().eq("user_id", user_id).execute()
        for item in payload.project_access:
            supabase.table("project_access").insert(
                {"user_id": user_id, "project_id": item.project_id, "role": item.role}
            ).execute()

    return {"message": "Usuario actualizado"}


# ---------------------------------------------------------
# DELETE /admin/users/{id} — elimina el usuario por completo
# (auth.users, y en cascada: profiles, project_access).
# ---------------------------------------------------------
@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    if user_id == auth["user"].id:
        raise HTTPException(status_code=400, detail="No puedes eliminar tu propia cuenta")

    try:
        supabase.auth.admin.delete_user(user_id)
    except Exception:
        logger.exception("No se pudo eliminar el usuario %s", user_id)
        raise HTTPException(status_code=400, detail="No se pudo eliminar el usuario")


# ---------------------------------------------------------
# Vistas por proyecto — reemplaza la pantalla que antes vivía dentro
# de cada proyecto (ej. Datalume → Ajustes → Usuarios) para decidir
# qué secciones del panel puede ver cada analista. Ahora se administra
# desde aquí: el admin elige el proyecto y ve/edita los permisos de
# cada uno de sus analistas (uno por analista, no uno solo para todos).
# ---------------------------------------------------------
DEFAULT_PERMISSIONS = {
    "ventas": True,
    "ventas_resumen": True,
    "ventas_clientes": True,
    "ventas_comparacion": True,
    "cargar": False,
    "explorar": False,
    "reportes": True,
}


class ProjectAnalystPermissionsInput(BaseModel):
    ventas: bool
    ventas_resumen: bool
    ventas_clientes: bool
    ventas_comparacion: bool
    cargar: bool
    explorar: bool
    reportes: bool


@router.get("/projects/{project_id}/analysts")
def list_project_analysts(project_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    access_rows = (
        supabase.table("project_access")
        .select("user_id")
        .eq("project_id", project_id)
        .eq("role", "analyst")
        .execute()
    ).data or []
    analyst_ids = [row["user_id"] for row in access_rows]
    if not analyst_ids:
        return []

    profiles = (
        supabase.table("profiles")
        .select("id, full_name, phone, created_at")
        .in_("id", analyst_ids)
        .execute()
    ).data or []

    perms = (
        supabase.table("analyst_permissions")
        .select("*")
        .in_("user_id", analyst_ids)
        .execute()
    ).data or []
    perms_by_id = {row["user_id"]: row for row in perms}

    try:
        page = supabase.auth.admin.list_users()
        auth_users = page.users if hasattr(page, "users") else (page or [])
        emails = {str(u.id): u.email for u in auth_users}
    except Exception:
        logger.exception("No se pudo listar usuarios de auth")
        emails = {}

    return [
        {
            "id": p["id"],
            "full_name": p.get("full_name"),
            "email": emails.get(p["id"]),
            "phone": p.get("phone"),
            "created_at": p.get("created_at"),
            "permissions": perms_by_id.get(p["id"], {**DEFAULT_PERMISSIONS, "user_id": p["id"]}),
        }
        for p in profiles
    ]


@router.put("/projects/{project_id}/analysts/{analyst_id}/permissions")
def update_project_analyst_permissions(
    project_id: str,
    analyst_id: str,
    payload: ProjectAnalystPermissionsInput,
    auth=Depends(require_role("admin")),
):
    supabase = get_supabase_admin()

    owned = (
        supabase.table("project_access")
        .select("user_id")
        .eq("project_id", project_id)
        .eq("user_id", analyst_id)
        .eq("role", "analyst")
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(
            status_code=404,
            detail="Ese analista no tiene acceso de analista a este proyecto",
        )

    data = {"user_id": analyst_id, "created_by": auth["user"].id, **payload.model_dump()}
    result = supabase.table("analyst_permissions").upsert(data, on_conflict="user_id").execute()
    return result.data[0] if result.data else data


# ---------------------------------------------------------
# Datasets por analista — qué archivos concretos (CSV ya cargados)
# puede ver cada analista, controlado también desde el Portal en vez
# de desde dentro de cada proyecto. Se apoya en el "entorno de datos"
# (projects.env_owner_id) que vincula un proyecto del Portal con el
# usuario dueño real de esos datasets.
# ---------------------------------------------------------
def _require_project_analyst_and_env(supabase, project_id: str, analyst_id: str) -> str:
    project = (
        supabase.table("projects").select("id, env_owner_id").eq("id", project_id).limit(1).execute()
    )
    if not project.data or not project.data[0].get("env_owner_id"):
        raise HTTPException(
            status_code=400,
            detail="Este proyecto todavía no tiene un entorno de datos vinculado",
        )

    owned = (
        supabase.table("project_access")
        .select("user_id")
        .eq("project_id", project_id)
        .eq("user_id", analyst_id)
        .eq("role", "analyst")
        .limit(1)
        .execute()
    )
    if not owned.data:
        raise HTTPException(
            status_code=404,
            detail="Ese analista no tiene acceso de analista a este proyecto",
        )

    return project.data[0]["env_owner_id"]


@router.get("/projects/{project_id}/analysts/{analyst_id}/datasets")
def list_project_analyst_datasets(project_id: str, analyst_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    env_owner_id = _require_project_analyst_and_env(supabase, project_id, analyst_id)

    datasets = (
        supabase.table("datasets")
        .select("id, file_name, created_at")
        .eq("user_id", env_owner_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    access = (
        supabase.table("analyst_dataset_access")
        .select("dataset_id")
        .eq("analyst_id", analyst_id)
        .execute()
    ).data or []
    allowed_ids = {row["dataset_id"] for row in access}

    return [
        {"id": d["id"], "file_name": d["file_name"], "allowed": d["id"] in allowed_ids}
        for d in datasets
    ]


class ProjectAnalystDatasetsInput(BaseModel):
    dataset_ids: list[str] = Field(default_factory=list)


@router.put("/projects/{project_id}/analysts/{analyst_id}/datasets")
def update_project_analyst_datasets(
    project_id: str,
    analyst_id: str,
    payload: ProjectAnalystDatasetsInput,
    auth=Depends(require_role("admin")),
):
    supabase = get_supabase_admin()
    env_owner_id = _require_project_analyst_and_env(supabase, project_id, analyst_id)

    # Nunca confiar en los ids tal cual: solo se permiten datasets que de
    # verdad pertenecen al entorno de datos de este proyecto.
    valid_ids: list[str] = []
    if payload.dataset_ids:
        valid = (
            supabase.table("datasets")
            .select("id")
            .eq("user_id", env_owner_id)
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