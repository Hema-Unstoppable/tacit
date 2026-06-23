from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


class Settings:
    app_secret: str = os.getenv("APP_SECRET", "dev-secret-change-me")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/tacit.db")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.5")
    upload_dir: Path = Path(os.getenv("UPLOAD_DIR", "./data/uploads"))
    app_timezone: str = os.getenv("APP_TIMEZONE", "Asia/Dubai")
    linkedin_client_id: str | None = os.getenv("LINKEDIN_CLIENT_ID") or None
    linkedin_client_secret: str | None = os.getenv("LINKEDIN_CLIENT_SECRET") or None
    linkedin_redirect_uri: str = os.getenv(
        "LINKEDIN_REDIRECT_URI",
        "http://127.0.0.1:8000/linkedin/callback",
    )
    linkedin_api_version: str = os.getenv("LINKEDIN_API_VERSION", "202605")
    linkedin_auto_publish: bool = os.getenv("LINKEDIN_AUTO_PUBLISH", "false").lower() == "true"
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_pass: str = os.getenv("SMTP_PASS", "")
    smtp_from: str = os.getenv("SMTP_FROM", "")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")
    google_client_id: str | None = os.getenv("GOOGLE_CLIENT_ID") or None
    google_client_secret: str | None = os.getenv("GOOGLE_CLIENT_SECRET") or None
    google_redirect_uri: str = os.getenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback")


settings = Settings()
