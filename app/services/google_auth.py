from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx

from app.config import settings

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def google_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def build_google_auth_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{_AUTH_URL}?{urlencode(params)}"


def exchange_google_code(code: str) -> dict:
    with httpx.Client(timeout=30) as client:
        response = client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        response.raise_for_status()
        return response.json()


def get_google_user_info(access_token: str) -> dict:
    with httpx.Client(timeout=30) as client:
        response = client.get(
            _USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()
