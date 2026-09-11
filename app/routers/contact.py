import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.config.settings import get_supabase_admin
from app.utils.auth_dependency import require_role
from app.utils.mailer import send_contact_alert_email, send_contact_reply_email

router = APIRouter(prefix="/contact", tags=["contact"])

logger = logging.getLogger("datalume.contact")


# ---------------------------------------------------------
# POST /contact — endpoint PÚBLICO (sin login). Lo usa el formulario
# de contacto de la landing. Guarda el mensaje y avisa por correo al
# administrador (ADMIN_EMAIL); si el correo falla, el mensaje igual
# queda guardado (se puede ver desde el panel de Contactos).
# ---------------------------------------------------------
class ContactMessageInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    company: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=5000)


@router.post("", status_code=201)
def create_contact_message(payload: ContactMessageInput):
    supabase = get_supabase_admin()

    row = {
        "name": payload.name.strip(),
        "email": str(payload.email).lower().strip(),
        "company": (payload.company or "").strip() or None,
        "message": payload.message.strip(),
    }

    result = supabase.table("contact_messages").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="No se pudo guardar el mensaje")

    try:
        send_contact_alert_email(row["name"], row["email"], row["company"], row["message"])
    except Exception:
        logger.exception("No se pudo enviar la alerta de contacto al administrador")

    return {"message": "Mensaje enviado, te responderemos pronto."}


# ---------------------------------------------------------
# GET /contact — lista todos los mensajes (solo admin), del más
# reciente al más antiguo.
# ---------------------------------------------------------
@router.get("")
def list_contact_messages(auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()
    result = (
        supabase.table("contact_messages")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------
# POST /contact/{id}/reply — el admin responde un mensaje. Guarda la
# respuesta y la envía por correo a la dirección que la persona dejó
# en el formulario.
# ---------------------------------------------------------
class ContactReplyInput(BaseModel):
    reply: str = Field(min_length=1, max_length=5000)


@router.post("/{message_id}/reply")
def reply_contact_message(message_id: str, payload: ContactReplyInput, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    existing = (
        supabase.table("contact_messages")
        .select("id, name, email, message")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    original = existing.data[0]
    reply_text = payload.reply.strip()

    update = {
        "status": "answered",
        "admin_reply": reply_text,
        "replied_by": auth["user"].id,
        "replied_at": datetime.now(timezone.utc).isoformat(),
    }
    result = supabase.table("contact_messages").update(update).eq("id", message_id).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="No se pudo guardar la respuesta")

    try:
        send_contact_reply_email(original["email"], original["name"], original["message"], reply_text)
    except Exception:
        logger.exception("No se pudo enviar el correo de respuesta a %s", original["email"])
        raise HTTPException(
            status_code=502,
            detail="La respuesta se guardó, pero no se pudo enviar el correo. Intenta reenviarla.",
        )

    return result.data[0]


# ---------------------------------------------------------
# DELETE /contact/{id} — el admin borra un mensaje de contacto
# (ya sea pendiente o respondido).
# ---------------------------------------------------------
@router.delete("/{message_id}", status_code=204)
def delete_contact_message(message_id: str, auth=Depends(require_role("admin"))):
    supabase = get_supabase_admin()

    existing = (
        supabase.table("contact_messages")
        .select("id")
        .eq("id", message_id)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")

    supabase.table("contact_messages").delete().eq("id", message_id).execute()
    return None