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
    project_access: list[ProjectRoleInput] | None = None  # si viene, reemplaza el acceso completo


@router.patch("/users/{user_id}")
def update_user(user_id: str, payload: UserUpdateInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    if payload.role is not None:
        result = supabase.table("profiles").update({"role": payload.role}).eq("id", user_id).execute()
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