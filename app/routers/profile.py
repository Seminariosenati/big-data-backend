from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_auth

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    full_name: str | None = None
    company: str | None = None
    phone: str | None = None
    role: str | None = None


@router.get("/me")
def get_my_profile(auth=Depends(require_auth)):
    user = auth["user"]
    supabase = get_supabase_admin()

    result = supabase.table("profiles").select("*").eq("id", user.id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")

    return {"id": user.id, "email": user.email, **result.data}


@router.put("/me")
def update_my_profile(payload: ProfileUpdate, auth=Depends(require_auth)):
    user = auth["user"]
    supabase = get_supabase_admin()

    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No hay cambios para guardar")

    result = supabase.table("profiles").update(updates).eq("id", user.id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")

    return {"id": user.id, "email": user.email, **result.data[0]}