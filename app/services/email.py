from __future__ import annotations

import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_SALT = "tacit-email-verify-v1"
_MAX_AGE = 86400  # 24 hours


def generate_verification_token(email: str) -> str:
    s = URLSafeTimedSerializer(settings.app_secret)
    return s.dumps(email.lower().strip(), salt=_SALT)


def verify_email_token(token: str) -> str | None:
    s = URLSafeTimedSerializer(settings.app_secret)
    try:
        return s.loads(token, salt=_SALT, max_age=_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None


def send_verification_email(to_email: str, token: str) -> None:
    verify_url = f"{settings.app_base_url}/verify-email/{token}"

    if not settings.smtp_host:
        print(f"\n[TACIT EMAIL — DEV MODE]\nVerification link for {to_email}:\n{verify_url}\n")
        return

    html = f"""
    <html><body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f8fafc;margin:0;padding:40px 20px">
      <div style="background:#fff;border-radius:16px;max-width:520px;margin:0 auto;padding:48px 40px;border:1px solid #e5e7eb">
        <div style="text-align:center;margin-bottom:32px">
          <div style="background:linear-gradient(135deg,#1a73e8,#0a55a8);border-radius:10px;display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;color:#fff;font-weight:800;font-size:22px;font-family:'Helvetica Neue',Arial,sans-serif">T</div>
        </div>
        <h2 style="color:#0d1b2a;font-size:22px;font-weight:700;margin:0 0 12px;text-align:center">Verify your email address</h2>
        <p style="color:#6b7280;font-size:15px;line-height:1.6;margin:0 0 32px;text-align:center">
          Click the button below to verify your email and activate your Tacit account.<br>This link expires in <strong>24 hours</strong>.
        </p>
        <div style="text-align:center;margin-bottom:36px">
          <a href="{verify_url}" style="background:linear-gradient(135deg,#1a73e8,#0a55a8);color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:600;font-size:15px;display:inline-block">
            Verify my email →
          </a>
        </div>
        <p style="color:#9ca3af;font-size:13px;text-align:center;margin:0">
          If you didn't sign up for Tacit, you can safely ignore this email.
        </p>
      </div>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Verify your Tacit account"
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(msg["From"], [to_email], msg.as_string())
    except Exception as exc:
        print(f"[TACIT EMAIL ERROR] Failed to send to {to_email}: {exc}")
