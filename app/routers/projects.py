import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_auth

router = APIRouter(prefix="/projects", tags=["projects"])

logger = logging.getLogger("datalume.projects")

PROJECT_FIELDS = "id, slug, name, description, path, tag, accent, sort_order"


# ---------------------------------------------------------
# GET /projects
# Lista de proyectos para mostrar como tarjetas en el portal.
# Reemplaza el array hardcodeado que vivía en PortalPage.tsx.
#
# - Un admin ve todos los proyectos activos.
# - Un usuario normal solo ve los proyectos a los que tiene
#   acceso en `project_access` (tabla que ya existía en la
#   base de datos — es el control de acceso por proyecto del
#   punto 6 del plan, aquí ya lo dejamos conectado).
# - Se excluyen filas sin `path` configurado: significa que
#   el proyecto todavía no tiene una ruta real en el frontend
#   (por ejemplo, uno recién creado en `projects` a mano pero
#   cuyo código React todavía no existe).
# ---------------------------------------------------------
@router.get("")
def list_projects(auth=Depends(require_auth)):
    supabase = get_supabase_admin()
    user = auth["user"]

    if auth["role"] == "admin":
        result = (
            supabase.table("projects")
            .select(PROJECT_FIELDS)
            .eq("is_active", True)
            .not_.is_("path", "null")
            .order("sort_order", desc=False)
            .execute()
        )
        return result.data or []

    access = (
        supabase.table("project_access")
        .select("project_id")
        .eq("user_id", user.id)
        .execute()
    )
    project_ids = [row["project_id"] for row in (access.data or []) if row.get("project_id")]

    if not project_ids:
        return []

    result = (
        supabase.table("projects")
        .select(PROJECT_FIELDS)
        .eq("is_active", True)
        .not_.is_("path", "null")
        .in_("id", project_ids)
        .order("sort_order", desc=False)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------
# POST /projects/requests
# Opción A del punto 4 del plan: registra el interés en un
# proyecto nuevo. No crea nada automáticamente; el admin revisa
# esta tabla (o una futura pantalla) y lo agrega a mano.
# ---------------------------------------------------------
class ProjectRequestInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    contact_email: EmailStr


@router.post("/requests", status_code=201)
def create_project_request(payload: ProjectRequestInput, auth=Depends(require_auth)):
    supabase = get_supabase_admin()
    user = auth["user"]

    try:
        result = (
            supabase.table("project_requests")
            .insert(
                {
                    "name": payload.name.strip(),
                    "description": payload.description.strip(),
                    "contact_email": str(payload.contact_email).lower().strip(),
                    "requested_by": user.id,
                    "status": "pending",
                }
            )
            .execute()
        )
    except Exception:
        logger.exception("No se pudo registrar la solicitud de proyecto")
        raise HTTPException(status_code=400, detail="No se pudo registrar la solicitud")

    if not result.data:
        raise HTTPException(status_code=400, detail="No se pudo registrar la solicitud")

    return result.data[0]