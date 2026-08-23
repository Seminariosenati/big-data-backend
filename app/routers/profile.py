from fastapi import APIRouter, Depends, HTTPException

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_auth

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/me")
def get_my_profile(auth=Depends(require_auth)):
    user = auth["user"]
    supabase = get_supabase_admin()

    result = supabase.table("profiles").select("*").eq("id", user.id).single().execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")

    return {"id": user.id, "email": user.email, **result.data}
