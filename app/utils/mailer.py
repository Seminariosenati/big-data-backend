import requests

from app.config.settings import get_settings

EMAILJS_ENDPOINT = "https://api.emailjs.com/api/v1.0/email/send"


def send_otp_email(to_email: str, code: str) -> None:
    """Envía el código OTP usando la API REST de EmailJS.

    Render bloquea las conexiones SMTP salientes (puertos 25/465/587) en el
    plan gratuito, por eso no usamos smtplib. EmailJS envía por HTTPS (443)
    y su propio servidor es el que se conecta a Gmail por detrás, así que
    el bloqueo de puertos de Render no afecta.

    Requiere que en el dashboard de EmailJS:
      1. Se haya creado un "Email Service" conectado a tu cuenta de Gmail.
      2. Se haya creado un "Email Template" con las variables:
         {{to_email}}, {{code}}, {{expiration_minutes}}
      3. Se haya activado "API requests for non-browser applications"
         en Account -> Security (si no, EmailJS rechaza la llamada).
    """
    settings = get_settings()

    payload = {
        "service_id": settings.emailjs_service_id,
        "template_id": settings.emailjs_template_id,
        "user_id": settings.emailjs_public_key,
        "accessToken": settings.emailjs_private_key,
        "template_params": {
            "to_email": to_email,
            "code": code,
            "expiration_minutes": settings.otp_expiration_minutes,
        },
    }

    response = requests.post(EMAILJS_ENDPOINT, json=payload, timeout=10)

    if response.status_code != 200:
        raise RuntimeError(
            f"EmailJS respondió {response.status_code}: {response.text}"
        )


def send_invitation_email(to_email: str, project_name: str, login_url: str) -> None:
    """Envía el correo de invitación a un proyecto usando la misma API de EmailJS."""
    settings = get_settings()

    payload = {
        "service_id": settings.emailjs_service_id,
        "template_id": settings.emailjs_invitation_template_id,
        "user_id": settings.emailjs_public_key,
        "accessToken": settings.emailjs_private_key,
        "template_params": {
            "to_email": to_email,
            "project_name": project_name,
            "login_url": login_url,
        },
    }

    response = requests.post(EMAILJS_ENDPOINT, json=payload, timeout=10)

    if response.status_code != 200:
        raise RuntimeError(f"EmailJS respondió {response.status_code}: {response.text}")


def send_contact_alert_email(name: str, from_email: str, company: str | None, message: str) -> None:
    """Avisa al administrador (ADMIN_EMAIL) que llegó un mensaje nuevo desde
    el formulario público de contacto, usando la misma API de EmailJS.

    Requiere un "Email Template" en EmailJS con variables:
    {{to_email}}, {{sender_name}}, {{sender_email}}, {{sender_company}}, {{message}}
    """
    settings = get_settings()

    payload = {
        "service_id": settings.emailjs_contact_service_id,
        "template_id": settings.emailjs_contact_alert_template_id,
        "user_id": settings.emailjs_contact_public_key,
        "accessToken": settings.emailjs_contact_private_key,
        "template_params": {
            "to_email": settings.admin_email,
            "sender_name": name,
            "sender_email": from_email,
            "sender_company": company or "(no especificada)",
            "message": message,
        },
    }

    response = requests.post(EMAILJS_ENDPOINT, json=payload, timeout=10)

    if response.status_code != 200:
        raise RuntimeError(f"EmailJS respondió {response.status_code}: {response.text}")


def send_contact_reply_email(to_email: str, name: str, original_message: str, reply: str) -> None:
    """Envía la respuesta del admin al correo que la persona dejó en el
    formulario de contacto, usando la misma API de EmailJS.

    Requiere un "Email Template" en EmailJS con variables:
    {{to_email}}, {{sender_name}}, {{original_message}}, {{reply}}
    """
    settings = get_settings()

    payload = {
        "service_id": settings.emailjs_contact_service_id,
        "template_id": settings.emailjs_contact_reply_template_id,
        "user_id": settings.emailjs_contact_public_key,
        "accessToken": settings.emailjs_contact_private_key,
        "template_params": {
            "to_email": to_email,
            "sender_name": name,
            "original_message": original_message,
            "reply": reply,
        },
    }

    response = requests.post(EMAILJS_ENDPOINT, json=payload, timeout=10)

    if response.status_code != 200:
        raise RuntimeError(f"EmailJS respondió {response.status_code}: {response.text}")