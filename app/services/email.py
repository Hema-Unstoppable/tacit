from __future__ import annotations

import hashlib
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

_SALT = "tacit-email-verify-v1"
_MAX_AGE = 86400  # 24 hours
_RESET_SALT = "tacit-password-reset-v1"
_RESET_MAX_AGE = 3600  # 1 hour


def generate_verification_token(email: str) -> str:
    s = URLSafeTimedSerializer(settings.app_secret)
    return s.dumps(email.lower().strip(), salt=_SALT)


def verify_email_token(token: str) -> str | None:
    s = URLSafeTimedSerializer(settings.app_secret)
    try:
        return s.loads(token, salt=_SALT, max_age=_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None


def password_reset_fingerprint(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def generate_password_reset_token(email: str, password_hash: str) -> str:
    s = URLSafeTimedSerializer(settings.app_secret)
    fingerprint = password_reset_fingerprint(password_hash)
    return s.dumps(f"{email.lower().strip()}|{fingerprint}", salt=_RESET_SALT)


def parse_password_reset_token(token: str) -> tuple[str, str] | None:
    """Verifies signature/expiry and returns (email, fingerprint). The fingerprint
    must still be compared against the user's current password hash by the caller —
    this is what makes the token single-use without a server-side blocklist."""
    s = URLSafeTimedSerializer(settings.app_secret)
    try:
        payload = s.loads(token, salt=_RESET_SALT, max_age=_RESET_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return None
    try:
        email, fingerprint = payload.rsplit("|", 1)
    except ValueError:
        return None
    return email, fingerprint


def _email_card(heading: str, body_html: str, button_url: str, button_label: str, footer: str) -> str:
    return f"""
    <html><body style="font-family:'Helvetica Neue',Arial,sans-serif;background:#f8fafc;margin:0;padding:40px 20px">
      <div style="background:#fff;border-radius:16px;max-width:520px;margin:0 auto;padding:48px 40px;border:1px solid #e5e7eb">
        <div style="text-align:center;margin-bottom:32px">
          <div style="background:linear-gradient(135deg,#1a73e8,#0a55a8);border-radius:10px;display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;color:#fff;font-weight:800;font-size:22px;font-family:'Helvetica Neue',Arial,sans-serif">T</div>
        </div>
        <h2 style="color:#0d1b2a;font-size:22px;font-weight:700;margin:0 0 12px;text-align:center">{heading}</h2>
        <p style="color:#6b7280;font-size:15px;line-height:1.6;margin:0 0 32px;text-align:center">{body_html}</p>
        <div style="text-align:center;margin-bottom:36px">
          <a href="{button_url}" style="background:linear-gradient(135deg,#1a73e8,#0a55a8);color:#fff;text-decoration:none;padding:14px 32px;border-radius:10px;font-weight:600;font-size:15px;display:inline-block">
            {button_label}
          </a>
        </div>
        <p style="color:#9ca3af;font-size:13px;text-align:center;margin:0">{footer}</p>
      </div>
    </body></html>
    """


def _send_html_email(to_email: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from or settings.smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(settings.smtp_user, settings.smtp_pass)
            server.sendmail(msg["From"], [to_email], msg.as_string())
    except Exception as exc:
        print(f"[TACIT EMAIL ERROR] Failed to send to {to_email}: {exc}")


def send_verification_email(to_email: str, token: str) -> None:
    verify_url = f"{settings.app_base_url}/verify-email/{token}"

    if not settings.smtp_host:
        print(f"\n[TACIT EMAIL — DEV MODE]\nVerification link for {to_email}:\n{verify_url}\n")
        return

    html = _email_card(
        heading="Verify your email address",
        body_html="Click the button below to verify your email and activate your Tacit account.<br>This link expires in <strong>24 hours</strong>.",
        button_url=verify_url,
        button_label="Verify my email →",
        footer="If you didn't sign up for Tacit, you can safely ignore this email.",
    )
    _send_html_email(to_email, "Verify your Tacit account", html)


def send_password_reset_email(to_email: str, token: str) -> None:
    reset_url = f"{settings.app_base_url}/reset-password/{token}"

    if not settings.smtp_host:
        print(f"\n[TACIT EMAIL — DEV MODE]\nPassword reset link for {to_email}:\n{reset_url}\n")
        return

    html = _email_card(
        heading="Reset your password",
        body_html="Click the button below to choose a new password.<br>This link expires in <strong>1 hour</strong>.",
        button_url=reset_url,
        button_label="Reset my password →",
        footer="If you didn't request this, you can safely ignore this email — your password will not change.",
    )
    _send_html_email(to_email, "Reset your Tacit password", html)
