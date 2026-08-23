import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config.settings import get_settings


def send_otp_email(to_email: str, code: str) -> None:
    settings = get_settings()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Tu código de verificación — Datalume"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to_email

    text = f"Tu código de verificación es: {code}. Vence en {settings.otp_expiration_minutes} minutos."
    html = f"""
    <div style="font-family: sans-serif; max-width: 420px; margin: 0 auto;">
      <h2 style="color:#111;">Verifica tu inicio de sesión</h2>
      <p style="color:#444;">Usa este código para completar tu inicio de sesión en Datalume:</p>
      <div style="font-size: 32px; font-weight: 700; letter-spacing: 6px; background:#f4f4f5; padding: 16px 24px; border-radius: 8px; text-align:center; margin: 16px 0;">
        {code}
      </div>
      <p style="color:#888; font-size: 13px;">Este código vence en {settings.otp_expiration_minutes} minutos. Si no fuiste tú, ignora este correo.</p>
    </div>
    """

    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.starttls()
        server.login(settings.smtp_user, settings.smtp_pass)
        server.sendmail(msg["From"], [to_email], msg.as_string())
