from fastapi import Depends, Header, HTTPException, status

from app.config.settings import get_supabase_admin


async def require_auth(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No autenticado")

    token = authorization.removeprefix("Bearer ").strip()
    supabase = get_supabase_admin()

    try:
        result = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida o expirada")

    if not result or not result.user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesión inválida o expirada")

    # Trae el rol guardado en profiles (admin | analyst). Si por algún motivo
    # el perfil no existe o no tiene rol, se asume 'admin' para no romper a
    # los usuarios que ya existían antes de introducir roles.
    supabase_admin = get_supabase_admin()
    profile = (
        supabase_admin.table("profiles")
        .select("role, company, owner_id")
        .eq("id", result.user.id)
        .limit(1)
        .execute()
    )
    role = "admin"
    company = None
    owner_id = None
    if profile.data:
        role = profile.data[0].get("role") or "admin"
        company = profile.data[0].get("company")
        owner_id = profile.data[0].get("owner_id")

    return {
        "user": result.user,
        "access_token": token,
        "role": role,
        "company": company,
        # Para un analista, owner_id es el admin dueño del sistema al que
        # pertenece (de ahí saca sus permisos). Para un admin, es None:
        # un admin siempre es dueño de sí mismo.
        "owner_id": owner_id,
    }


def require_role(*allowed_roles: str):
    """Dependencia para restringir un endpoint a ciertos roles.

    Uso: Depends(require_role("admin"))  -> solo admin
         Depends(require_role("admin", "analyst")) -> ambos, pero deja el
         rol disponible en auth["role"] para que el endpoint decida qué
         datos devolver (ej. el analyst nunca ve columnas de estructura
         interna, solo datos ya agregados).
    """

    async def checker(auth: dict = Depends(require_auth)):
        if auth["role"] not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permiso para acceder a este recurso",
            )
        return auth

    return checker