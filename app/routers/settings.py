from fastapi import APIRouter, Depends

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_auth

router = APIRouter(prefix="/settings", tags=["settings"])

DEFAULT_PERMISSIONS = {
    "ventas": True,
    "ventas_resumen": True,
    "ventas_clientes": True,
    "ventas_comparacion": True,
    "cargar": False,
    "explorar": False,
    "reportes": True,
}


@router.get("/analyst-permissions")
def get_analyst_permissions(auth=Depends(require_auth)):
    """Un admin siempre tiene acceso total. Un analista lee sus propios
    permisos individuales (tabla analyst_permissions, PK user_id), asignados
    por su admin desde /users."""
    if auth["role"] == "admin":
        return {"user_id": auth["user"].id, **{k: True for k in DEFAULT_PERMISSIONS}}

    supabase = get_supabase_admin()
    result = (
        supabase.table("analyst_permissions")
        .select("*")
        .eq("user_id", auth["user"].id)
        .limit(1)
        .execute()
    )

    if not result.data:
        return {"user_id": auth["user"].id, **DEFAULT_PERMISSIONS}

    return result.data[0]