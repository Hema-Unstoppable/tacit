from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.config import settings


AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
ORG_ACLS_URL = "https://api.linkedin.com/rest/organizationAcls"
ORG_URL = "https://api.linkedin.com/rest/organizations"


class LinkedInConfigError(RuntimeError):
    pass


def linkedin_configured() -> bool:
    return bool(settings.linkedin_client_id and settings.linkedin_client_secret)


def build_authorization_url() -> tuple[str, str]:
    if not linkedin_configured():
        raise LinkedInConfigError("LinkedIn Client ID and Client Secret are not configured.")
    state = secrets.token_urlsafe(24)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.linkedin_client_id,
            "redirect_uri": settings.linkedin_redirect_uri,
            "state": state,
            "scope": "openid profile email w_member_social w_organization_social r_organization_social",
        }
    )
    return f"{AUTH_URL}?{query}", state


def exchange_code_for_token(code: str) -> dict:
    if not linkedin_configured():
        raise LinkedInConfigError("LinkedIn Client ID and Client Secret are not configured.")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.linkedin_redirect_uri,
        "client_id": settings.linkedin_client_id,
        "client_secret": settings.linkedin_client_secret,
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(TOKEN_URL, data=data)
        response.raise_for_status()
        return response.json()


def fetch_userinfo(access_token: str) -> dict:
    with httpx.Client(timeout=30) as client:
        response = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
        response.raise_for_status()
        return response.json()


def token_expiry(expires_in: int | None) -> datetime | None:
    if not expires_in:
        return None
    return datetime.utcnow() + timedelta(seconds=max(0, int(expires_in) - 300))


def publish_text_post(access_token: str, author_urn: str, text: str) -> str:
    payload = {
        "author": author_urn,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": settings.linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(POSTS_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.headers.get("x-restli-id", "")


def linkedin_post_url(post_id: str) -> str:
    if not post_id:
        return ""
    return f"https://www.linkedin.com/feed/update/{post_id}"


def fetch_managed_organizations(access_token: str) -> list[dict]:
    """Returns orgs the token holder is an ADMINISTRATOR of, with name if fetchable."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": settings.linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    try:
        with httpx.Client(timeout=20) as client:
            response = client.get(
                ORG_ACLS_URL,
                params={"q": "roleAssignee", "role": "ADMINISTRATOR"},
                headers=headers,
            )
            if response.status_code != 200:
                return []
            orgs = []
            for element in response.json().get("elements", []):
                urn = element.get("organization", "")
                if not urn:
                    continue
                name = _fetch_org_name(client, urn, headers)
                orgs.append({"urn": urn, "name": name or urn})
            return orgs
    except Exception:
        return []


def fetch_org_name(access_token: str, org_urn: str) -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": settings.linkedin_api_version,
        "X-Restli-Protocol-Version": "2.0.0",
    }
    with httpx.Client(timeout=20) as client:
        return _fetch_org_name(client, org_urn, headers)


def normalize_org_urn(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("urn:li:organization:"):
        return raw
    if raw.isdigit():
        return f"urn:li:organization:{raw}"
    return ""


def _fetch_org_name(client: httpx.Client, org_urn: str, headers: dict) -> str:
    org_id = org_urn.split(":")[-1]
    try:
        resp = client.get(
            f"{ORG_URL}/{org_id}",
            params={"projection": "(localizedName)"},
            headers=headers,
        )
        if resp.status_code == 200:
            return resp.json().get("localizedName", "")
    except Exception:
        pass
    return ""
