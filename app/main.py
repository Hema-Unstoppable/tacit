from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import OpenAIError, RateLimitError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import SessionLocal, get_db, init_db
from app.models import Insight, LinkedInAccount, Post, Source, User, VoiceProfile, Workspace, WorkspaceMember
from app.services.ai import get_ai_client
from app.services.documents import UnsupportedFileType, extract_text
from app.services.linkedin import (
    build_authorization_url,
    exchange_code_for_token,
    fetch_managed_organizations,
    fetch_org_name,
    fetch_userinfo,
    linkedin_configured,
    linkedin_post_url,
    normalize_org_urn,
    publish_text_post,
    token_expiry,
)
from app.services.email import generate_verification_token, send_verification_email, verify_email_token
from app.services.google_auth import build_google_auth_url, exchange_google_code, get_google_user_info, google_configured
from app.services.security import hash_password, verify_password


app = FastAPI(title="Tacit")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret,
    https_only=settings.session_https_only,
    same_site="lax",
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def format_app_datetime(value: datetime | None, pattern: str = "%b %d, %Y %I:%M %p") -> str:
    if not value:
        return ""
    return f"{value.strftime(pattern)} {settings.app_timezone}"


def linkedin_url(value: str | None) -> str:
    return linkedin_post_url(value or "")


templates.env.filters["app_datetime"] = format_app_datetime
templates.env.filters["linkedin_url"] = linkedin_url

COMMON_TIMEZONE_LABELS = [
    "Asia/Dubai",
    "Asia/Riyadh",
    "Asia/Kolkata",
    "Asia/Karachi",
    "Europe/London",
    "Europe/Paris",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Africa/Cairo",
    "Asia/Singapore",
    "Australia/Sydney",
]


@app.on_event("startup")
def on_startup() -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db()
    asyncio.create_task(scheduled_publish_loop())


async def scheduled_publish_loop() -> None:
    while True:
        try:
            if settings.linkedin_auto_publish:
                publish_due_posts_once()
        except Exception as exc:
            print(f"Scheduled publish loop error: {exc}")
        await asyncio.sleep(60)


def publish_due_posts_once(workspace_id: int | None = None) -> dict[str, int]:
    db = SessionLocal()
    published = 0
    failed = 0
    skipped = 0
    try:
        query = db.query(Post).filter(
            Post.status == "scheduled",
            Post.planned_publish_at.is_not(None),
            Post.planned_publish_at <= datetime.now(),
        )
        if workspace_id is not None:
            query = query.filter(Post.workspace_id == workspace_id)
        due_posts = query.order_by(Post.planned_publish_at.asc()).all()
        for post in due_posts:
            if publish_post(db, post):
                published += 1
            else:
                failed += 1
        db.commit()
        return {"published": published, "failed": failed, "skipped": skipped}
    finally:
        db.close()


def publish_post(db: Session, post: Post) -> bool:
    account = (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.workspace_id == post.workspace_id)
        .order_by(LinkedInAccount.updated_at.desc())
        .first()
    )
    if not account:
        post.status = "publish_failed"
        post.publish_error = "No connected LinkedIn account for this workspace."
        post.updated_at = datetime.utcnow()
        return False
    if account.token_expires_at and account.token_expires_at <= datetime.utcnow():
        post.status = "publish_failed"
        post.publish_error = "LinkedIn token expired. Reconnect LinkedIn from Channels."
        post.updated_at = datetime.utcnow()
        return False
    author_urn = account.org_urn if account.org_urn else account.linkedin_urn
    try:
        linkedin_post_id = publish_text_post(
            account.access_token,
            author_urn,
            post.draft_text,
        )
    except Exception as exc:
        post.status = "publish_failed"
        post.publish_error = str(exc)[:2000]
        post.updated_at = datetime.utcnow()
        return False

    post.status = "posted"
    post.linkedin_post_id = linkedin_post_id
    post.posted_at = datetime.utcnow()
    post.publish_error = ""
    post.updated_at = datetime.utcnow()
    return True


def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, int(user_id))


def require_verified_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if not user.email_verified:
        raise HTTPException(status_code=303, headers={"Location": "/verify-pending"})
    return user


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    if not user.email_verified:
        raise HTTPException(status_code=303, headers={"Location": "/verify-pending"})
    if not user.onboarding_complete:
        raise HTTPException(status_code=303, headers={"Location": "/onboarding"})
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = require_user(request, db)
    if not user.is_admin:
        raise HTTPException(status_code=404)
    return user


def current_workspace(user: User, db: Session) -> Workspace:
    membership = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    if not membership:
        workspace = Workspace(name="My Tacit Workspace")
        db.add(workspace)
        db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
        db.refresh(workspace)
        return workspace
    return membership.workspace


def first_voice_profile(workspace: Workspace, db: Session) -> VoiceProfile | None:
    return (
        db.query(VoiceProfile)
        .filter(VoiceProfile.workspace_id == workspace.id)
        .order_by(VoiceProfile.id.asc())
        .first()
    )


def ai_status() -> dict[str, str | bool]:
    configured = bool(settings.openai_api_key)
    return {
        "configured": configured,
        "label": "OpenAI connected" if configured else "Local fallback mode",
        "model": settings.openai_model if configured else "No API key",
    }


def linkedin_status(workspace: Workspace, db: Session) -> dict:
    account = (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.workspace_id == workspace.id)
        .order_by(LinkedInAccount.updated_at.desc())
        .first()
    )
    return {
        "configured": linkedin_configured(),
        "connected": bool(account),
        "account": account,
    }


def ai_error_message(exc: Exception) -> str:
    if isinstance(exc, RateLimitError):
        return "OpenAI is connected, but this API project has no available quota. Please add billing/credits or use another API key."
    if isinstance(exc, OpenAIError):
        return "OpenAI returned an API error. Please check the API key, project access, model, and billing settings."
    return "AI generation failed. Please try again or check the server logs."


def source_status_summary(source: Source) -> dict[str, str | int]:
    insights_count = len(source.insights)
    posts_count = len(source.posts)
    if source.status == "failed":
        label = "Extraction failed"
        tone = "publish_failed"
        description = "Tacit could not read this source."
    elif posts_count:
        label = "Drafts created"
        tone = "posted"
        description = f"{posts_count} draft posts created from {insights_count} insights."
    elif insights_count:
        label = "Insights available"
        tone = "ready_to_publish"
        description = f"{insights_count} insights ready for draft generation."
    else:
        label = "No insights yet"
        tone = "draft"
        description = "Ready to turn this source into LinkedIn drafts."
    return {
        "label": label,
        "tone": tone,
        "description": description,
        "insights_count": insights_count,
        "posts_count": posts_count,
    }


def build_source_preferences(
    content_length: str,
    emoji_style: str,
    post_structure: str,
    tone: str,
    custom_tone: str,
    content_goal: str,
    post_language: str,
    post_tags: str,
    extra_instructions: str,
    campaign_arc: str = "",
) -> str:
    tone_value = custom_tone.strip() if tone == "other" and custom_tone.strip() else tone
    preferences = {
        "length": content_length,
        "emoji_style": emoji_style,
        "structure": post_structure,
        "tone": tone_value,
        "goal": content_goal,
        "language": post_language,
        "tags": post_tags.strip(),
        "extra_instructions": extra_instructions.strip(),
    }
    if campaign_arc:
        preferences["campaign_arc"] = campaign_arc
    return json.dumps(preferences)


def source_preferences(source: Source) -> dict[str, str]:
    defaults = {
        "length": "medium",
        "emoji_style": "no_emojis",
        "structure": "hook_body_takeaway",
        "tone": "professional",
        "goal": "educate",
        "language": "english",
        "tags": "",
        "extra_instructions": "",
        "campaign_arc": "launch_sequence",
    }
    if not source.generation_preferences_json:
        return defaults
    try:
        parsed = json.loads(source.generation_preferences_json)
    except json.JSONDecodeError:
        return defaults
    return {**defaults, **{key: str(value) for key, value in parsed.items() if value is not None}}


def create_posts_for_insight(
    db: Session,
    insight: Insight,
    workspace: Workspace,
    profile: VoiceProfile | None,
) -> int:
    group_id = uuid.uuid4().hex
    generated = get_ai_client().generate_posts(insight, profile)
    for index, draft_text in enumerate(generated, start=1):
        db.add(
            Post(
                workspace_id=workspace.id,
                source_id=insight.source_id,
                insight_id=insight.id,
                voice_profile_id=profile.id if profile else None,
                variant_group_id=group_id,
                variant_number=index,
                draft_text=draft_text,
                status="draft",
            )
        )
    return len(generated)


def save_optional_upload(file: UploadFile | None) -> str:
    if not file or not file.filename:
        return ""
    suffix = Path(file.filename).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".docx", ".txt", ".md"}:
        return ""
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    destination = settings.upload_dir / safe_name
    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    return str(destination)


def build_campaign_brief(
    campaign_type: str,
    title: str,
    brand_context: str,
    target_audience: str,
    key_message: str,
    offer_details: str,
    call_to_action: str,
    logo_path: str,
    photo_path: str,
    style_samples: str = "",
) -> str:
    asset_notes = []
    if logo_path:
        asset_notes.append("Brand logo uploaded and available as a creative reference.")
    if photo_path:
        asset_notes.append("Post photo uploaded and available as a creative reference.")
    brief = "\n".join(
        [
            f"Campaign type: {campaign_type}",
            f"Campaign title: {title}",
            f"Brand context: {brand_context}",
            f"Target audience: {target_audience}",
            f"Key message: {key_message}",
            f"Offer/product/training details: {offer_details}",
            f"Call to action: {call_to_action}",
            f"Visual assets: {' '.join(asset_notes) if asset_notes else 'No visual assets uploaded.'}",
        ]
    )
    if style_samples.strip():
        brief += f"\n\n[STYLE REFERENCE — Match this brand voice exactly. Study the sentence rhythm, vocabulary, structure, and CTA style.]\n{style_samples.strip()}"
    return brief


def schedule_posts(
    posts: list[Post],
    frequency: str,
    every_days: int,
    time_mode: str,
    custom_time: str,
) -> None:
    if frequency != "every_n_days":
        every_days = 1
    if time_mode != "custom":
        custom_time = ""
    publish_time = schedule_time_for_mode(time_mode, custom_time)
    publish_date = first_schedule_date(frequency, publish_time)
    for post in posts:
        post.status = "scheduled"
        post.planned_publish_at = datetime.combine(publish_date, publish_time)
        post.scheduled_at = datetime.utcnow()
        post.updated_at = datetime.utcnow()
        publish_date = next_schedule_date(publish_date, frequency, every_days)


def schedule_time_for_mode(time_mode: str, custom_time: str) -> time:
    if time_mode == "rush":
        return time(12, 30)
    if time_mode == "custom" and custom_time:
        try:
            return time.fromisoformat(custom_time)
        except ValueError:
            return time(9, 0)
    return time(9, 0)


def next_schedule_date(current_date, frequency: str, every_days: int):
    if frequency == "weekly":
        return current_date + timedelta(days=7)
    if frequency == "weekends":
        candidate = current_date + timedelta(days=1)
        while candidate.weekday() not in {5, 6}:
            candidate += timedelta(days=1)
        return candidate
    if frequency == "every_n_days":
        return current_date + timedelta(days=max(1, every_days))
    return current_date + timedelta(days=1)


def first_schedule_date(frequency: str, publish_time: time):
    candidate = datetime.now().date()
    if datetime.combine(candidate, publish_time) <= datetime.now():
        candidate += timedelta(days=1)
    if frequency == "weekends":
        while candidate.weekday() not in {5, 6}:
            candidate += timedelta(days=1)
    return candidate


def order_posts_for_auto_schedule(posts: list[Post], ordering: str) -> list[Post]:
    if ordering == "source_sequence":
        return sorted(
            posts,
            key=lambda post: (
                (post.source.title.lower() if post.source else ""),
                post.created_at,
                post.variant_group_id,
                post.variant_number,
            ),
        )
    if ordering == "shuffle_sources":
        by_source: dict[int, list[Post]] = {}
        source_order: list[int] = []
        for post in sorted(posts, key=lambda item: (item.created_at, item.variant_group_id, item.variant_number)):
            source_id = post.source_id or 0
            if source_id not in by_source:
                by_source[source_id] = []
                source_order.append(source_id)
            by_source[source_id].append(post)
        ordered: list[Post] = []
        while any(by_source.values()):
            for source_id in source_order:
                if by_source[source_id]:
                    ordered.append(by_source[source_id].pop(0))
        return ordered
    return posts


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return templates.TemplateResponse("landing.html", {"request": request})
    workspace = current_workspace(user, db)
    voice_profile = first_voice_profile(workspace, db)
    sources = (
        db.query(Source)
        .filter(Source.workspace_id == workspace.id)
        .order_by(Source.created_at.desc())
        .limit(5)
        .all()
    )
    insights = (
        db.query(Insight)
        .filter(Insight.workspace_id == workspace.id)
        .order_by(Insight.created_at.desc())
        .limit(5)
        .all()
    )
    posts = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id)
        .order_by(Post.updated_at.desc())
        .limit(8)
        .all()
    )
    scheduled = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.status == "scheduled")
        .order_by(Post.planned_publish_at.asc())
        .limit(5)
        .all()
    )
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "voice_profile": voice_profile,
            "sources": sources,
            "insights": insights,
            "posts": posts,
            "scheduled": scheduled,
            "ai_status": ai_status(),
            "active": "dashboard",
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    workspace = current_workspace(user, db)
    all_users = db.query(User).order_by(User.created_at.desc()).all()
    rows = []
    for member_user in all_users:
        membership = (
            db.query(WorkspaceMember)
            .filter(WorkspaceMember.user_id == member_user.id)
            .order_by(WorkspaceMember.id.asc())
            .first()
        )
        member_workspace = membership.workspace if membership else None
        sources_count = (
            db.query(Source).filter(Source.workspace_id == member_workspace.id).count() if member_workspace else 0
        )
        posts_count = (
            db.query(Post).filter(Post.workspace_id == member_workspace.id).count() if member_workspace else 0
        )
        posted_count = (
            db.query(Post)
            .filter(Post.workspace_id == member_workspace.id, Post.status == "posted")
            .count()
            if member_workspace
            else 0
        )
        rows.append(
            {
                "user": member_user,
                "workspace": member_workspace,
                "sources_count": sources_count,
                "posts_count": posts_count,
                "posted_count": posted_count,
            }
        )
    totals = {
        "users": len(all_users),
        "verified": sum(1 for u in all_users if u.email_verified),
        "sources": db.query(Source).count(),
        "posts": db.query(Post).count(),
        "posted": db.query(Post).filter(Post.status == "posted").count(),
    }
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "rows": rows,
            "totals": totals,
            "ai_status": ai_status(),
            "active": "admin",
        },
    )


@app.get("/landing", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "signup"})


@app.post("/signup")
def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    workspace_name: str = Form("My Tacit Workspace"),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(User.email == email.lower().strip()).first()
    if existing:
        return templates.TemplateResponse(
            "auth.html",
            {"request": request, "mode": "signup", "error": "An account already exists for this email."},
            status_code=400,
        )
    user = User(email=email.lower().strip(), password_hash=hash_password(password), email_verified=False)
    workspace = Workspace(name=workspace_name.strip() or "My Tacit Workspace")
    db.add_all([user, workspace])
    db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="owner"))
    db.commit()
    request.session["user_id"] = user.id
    token = generate_verification_token(user.email)
    send_verification_email(user.email, token)
    return RedirectResponse("/verify-pending", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("auth.html", {"request": request, "mode": "login"})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.lower().strip()).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "auth.html",
            {"request": request, "mode": "login", "error": "Invalid email or password."},
            status_code=400,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/verify-pending", response_class=HTMLResponse)
def verify_pending(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user and user.email_verified:
        return RedirectResponse("/", status_code=303)
    dev_token = None
    if user and not settings.smtp_host:
        dev_token = generate_verification_token(user.email)
    return templates.TemplateResponse(
        "verify_pending.html",
        {
            "request": request,
            "pending_user": user,
            "dev_token": dev_token,
            "smtp_configured": bool(settings.smtp_host),
        },
    )


@app.get("/verify-email/{token}")
def verify_email(token: str, db: Session = Depends(get_db)):
    email = verify_email_token(token)
    if not email:
        return RedirectResponse("/login?error=Verification+link+is+invalid+or+has+expired.", status_code=303)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return RedirectResponse("/login?error=Account+not+found.", status_code=303)
    user.email_verified = True
    db.commit()
    return RedirectResponse("/verify-success", status_code=303)


@app.get("/verify-success", response_class=HTMLResponse)
def verify_success(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    return templates.TemplateResponse("verify_success.html", {"request": request, "pending_user": user})


@app.post("/resend-verification")
def resend_verification(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if user and not user.email_verified:
        token = generate_verification_token(user.email)
        send_verification_email(user.email, token)
    return RedirectResponse("/verify-pending?resent=1", status_code=303)


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding(request: Request, db: Session = Depends(get_db), user: User = Depends(require_verified_user)):
    if user.onboarding_complete:
        return RedirectResponse("/", status_code=303)
    workspace = current_workspace(user, db)
    has_voice = bool(first_voice_profile(workspace, db))
    has_source = db.query(Source).filter(Source.workspace_id == workspace.id).first() is not None
    last_source = (
        db.query(Source).filter(Source.workspace_id == workspace.id).order_by(Source.id.desc()).first()
        if has_source else None
    )
    if has_voice and has_source:
        step = 3
    elif has_voice:
        step = 2
    else:
        step = 1
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "ob_user": user,
            "step": step,
            "has_voice": has_voice,
            "has_source": has_source,
            "last_source": last_source,
        },
    )


@app.post("/onboarding/step/voice")
def onboarding_voice(
    request: Request,
    target_audience: str = Form(""),
    tone_notes: str = Form(""),
    example_post: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_verified_user),
):
    workspace = current_workspace(user, db)
    profile = first_voice_profile(workspace, db)
    if not profile:
        profile = VoiceProfile(workspace_id=workspace.id, name="My Voice")
        db.add(profile)
    profile.target_audience = target_audience
    profile.tone_notes = tone_notes
    profile.example_posts = example_post.strip()
    profile.updated_at = datetime.utcnow()
    profile.fingerprint_json = get_ai_client().fingerprint_voice(profile)
    db.commit()
    return RedirectResponse("/onboarding", status_code=303)


@app.post("/onboarding/step/source")
def onboarding_source(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_verified_user),
):
    workspace = current_workspace(user, db)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        return RedirectResponse("/onboarding?error=unsupported_file", status_code=303)
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    destination = settings.upload_dir / safe_name
    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    try:
        raw_text = extract_text(destination)
    except UnsupportedFileType:
        return RedirectResponse("/onboarding?error=unsupported_file", status_code=303)
    source = Source(
        workspace_id=workspace.id,
        title=title.strip() or Path(file.filename or "My first source").stem,
        source_type="upload",
        raw_text=raw_text,
        file_path=str(destination),
        original_filename=file.filename,
        content_type=file.content_type,
        generation_preferences_json=build_source_preferences(
            "medium", "no_emojis", "hook_body_takeaway", "professional", "", "educate", "english", "", ""
        ),
        status="ready" if raw_text.strip() else "failed",
    )
    db.add(source)
    db.commit()
    return RedirectResponse("/onboarding", status_code=303)


@app.post("/onboarding/complete")
def onboarding_complete(request: Request, db: Session = Depends(get_db), user: User = Depends(require_verified_user)):
    workspace = current_workspace(user, db)
    user.onboarding_complete = True
    db.commit()
    last_source = db.query(Source).filter(Source.workspace_id == workspace.id).order_by(Source.id.desc()).first()
    if last_source:
        return RedirectResponse(f"/sources/{last_source.id}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/onboarding/skip")
def onboarding_skip(request: Request, db: Session = Depends(get_db), user: User = Depends(require_verified_user)):
    user.onboarding_complete = True
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/auth/google")
def google_login(request: Request):
    if not google_configured():
        return RedirectResponse("/login?error=Google+sign-in+is+not+configured.", status_code=303)
    import secrets as _secrets
    state = _secrets.token_urlsafe(32)
    request.session["google_oauth_state"] = state
    return RedirectResponse(build_google_auth_url(state), status_code=303)


@app.get("/auth/google/callback")
def google_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    db: Session = Depends(get_db),
):
    if error or not code:
        return RedirectResponse("/login?error=Google+sign-in+was+cancelled.", status_code=303)
    stored_state = request.session.pop("google_oauth_state", None)
    if not stored_state or stored_state != state:
        return RedirectResponse("/login?error=Invalid+OAuth+state.+Please+try+again.", status_code=303)
    try:
        token_data = exchange_google_code(code)
        user_info = get_google_user_info(token_data["access_token"])
    except Exception:
        return RedirectResponse("/login?error=Google+sign-in+failed.+Please+try+again.", status_code=303)

    google_id = user_info.get("sub", "")
    email = (user_info.get("email") or "").lower().strip()
    name = user_info.get("name") or ""
    picture = user_info.get("picture") or ""

    if not email or not google_id:
        return RedirectResponse("/login?error=Could+not+retrieve+your+Google+account+details.", status_code=303)

    # Find existing user by google_id first, then by email
    user = db.query(User).filter(User.google_id == google_id).first()
    is_new_user = False
    if not user:
        user = db.query(User).filter(User.email == email).first()
        if user:
            # Link Google ID to existing email account
            user.google_id = google_id
            user.email_verified = True
            if name and not user.name:
                user.name = name
            if picture and not user.picture_url:
                user.picture_url = picture
            db.commit()
        else:
            # Brand new user via Google
            is_new_user = True
            user = User(
                email=email,
                password_hash="",
                email_verified=True,
                google_id=google_id,
                name=name,
                picture_url=picture,
            )
            workspace = Workspace(name=f"{name.split()[0]}'s Workspace" if name else "My Tacit Workspace")
            db.add_all([user, workspace])
            db.flush()
            db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="owner"))
            db.commit()
    else:
        # Returning Google user — refresh name/picture in case they changed
        if name:
            user.name = name
        if picture:
            user.picture_url = picture
        user.email_verified = True
        db.commit()

    request.session["user_id"] = user.id
    if is_new_user:
        return RedirectResponse("/voice", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/channels", response_class=HTMLResponse)
def channels_page(
    request: Request,
    linkedin_error: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    return templates.TemplateResponse(
        "channels.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "ai_status": ai_status(),
            "linkedin_status": linkedin_status(workspace, db),
            "linkedin_error": linkedin_error,
            "active": "channels",
        },
    )


@app.get("/linkedin/connect")
def linkedin_connect(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    try:
        authorization_url, state = build_authorization_url()
    except Exception as exc:
        return RedirectResponse(f"/channels?linkedin_error={quote(str(exc))}", status_code=303)
    request.session["linkedin_oauth_state"] = state
    return RedirectResponse(authorization_url, status_code=303)


@app.get("/linkedin/callback")
def linkedin_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    if error:
        message = error_description or error
        return RedirectResponse(f"/channels?linkedin_error={quote(message)}", status_code=303)
    expected_state = request.session.pop("linkedin_oauth_state", None)
    if not expected_state or state != expected_state:
        return RedirectResponse("/channels?linkedin_error=Invalid%20LinkedIn%20OAuth%20state.", status_code=303)
    if not code:
        return RedirectResponse("/channels?linkedin_error=LinkedIn%20did%20not%20return%20an%20authorization%20code.", status_code=303)

    workspace = current_workspace(user, db)
    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data["access_token"]
        profile = fetch_userinfo(access_token)
    except Exception as exc:
        return RedirectResponse(f"/channels?linkedin_error={quote(str(exc))}", status_code=303)

    linkedin_sub = profile.get("sub", "")
    linkedin_urn = f"urn:li:person:{linkedin_sub}" if linkedin_sub else ""
    if not linkedin_urn:
        return RedirectResponse("/channels?linkedin_error=LinkedIn%20profile%20did%20not%20include%20a%20member%20identifier.", status_code=303)

    account = (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.workspace_id == workspace.id, LinkedInAccount.linkedin_urn == linkedin_urn)
        .first()
    )
    if not account:
        account = LinkedInAccount(workspace_id=workspace.id, linkedin_urn=linkedin_urn, access_token=access_token)
        db.add(account)
    account.name = profile.get("name") or "LinkedIn member"
    account.email = profile.get("email") or ""
    account.picture_url = profile.get("picture") or ""
    account.access_token = access_token
    account.refresh_token = token_data.get("refresh_token")
    account.token_expires_at = token_expiry(token_data.get("expires_in"))
    account.updated_at = datetime.utcnow()
    # Auto-detect managed company pages if org scope was granted
    if not account.org_urn:
        try:
            orgs = fetch_managed_organizations(access_token)
            if orgs:
                account.org_urn = orgs[0]["urn"]
                account.org_name = orgs[0]["name"]
        except Exception:
            pass
    db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/linkedin/disconnect")
def linkedin_disconnect(db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    accounts = db.query(LinkedInAccount).filter(LinkedInAccount.workspace_id == workspace.id).all()
    for account in accounts:
        db.delete(account)
    db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/linkedin/set-org")
def linkedin_set_org(
    org_input: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    account = (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.workspace_id == workspace.id)
        .order_by(LinkedInAccount.updated_at.desc())
        .first()
    )
    if not account:
        return RedirectResponse("/channels?linkedin_error=No+LinkedIn+account+connected.", status_code=303)
    org_urn = normalize_org_urn(org_input)
    if not org_urn:
        return RedirectResponse("/channels?linkedin_error=Invalid+company+page+ID.+Enter+the+numeric+ID+or+full+URN.", status_code=303)
    try:
        org_name = fetch_org_name(account.access_token, org_urn) or org_urn
    except Exception:
        org_name = org_urn
    account.org_urn = org_urn
    account.org_name = org_name
    account.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.post("/linkedin/clear-org")
def linkedin_clear_org(db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    account = (
        db.query(LinkedInAccount)
        .filter(LinkedInAccount.workspace_id == workspace.id)
        .order_by(LinkedInAccount.updated_at.desc())
        .first()
    )
    if account:
        account.org_urn = ""
        account.org_name = ""
        account.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/channels", status_code=303)


@app.get("/voice", response_class=HTMLResponse)
def voice_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    profile = first_voice_profile(workspace, db)
    example_values = ["", "", ""]
    if profile and profile.example_posts:
        if "\n\n--- Example Post 2 ---\n\n" in profile.example_posts:
            first, remainder = profile.example_posts.split("\n\n--- Example Post 2 ---\n\n", 1)
            second, _, third = remainder.partition("\n\n--- Example Post 3 ---\n\n")
            example_values = [first, second, third]
        else:
            example_values[0] = profile.example_posts
    return templates.TemplateResponse(
        "voice.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "profile": profile,
            "example_values": example_values,
            "ai_status": ai_status(),
            "active": "voice",
        },
    )


@app.post("/voice")
def save_voice(
    request: Request,
    name: str = Form("Default voice"),
    target_audience: str = Form(""),
    tone_notes: str = Form(""),
    banned_phrases: str = Form(""),
    example_post_1: str = Form(""),
    example_post_2: str = Form(""),
    example_post_3: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    profile = first_voice_profile(workspace, db)
    if not profile:
        profile = VoiceProfile(workspace_id=workspace.id, name=name)
        db.add(profile)
    profile.name = name
    profile.target_audience = target_audience
    profile.tone_notes = tone_notes
    profile.banned_phrases = banned_phrases
    examples = [
        ("Example Post 1", example_post_1.strip()),
        ("Example Post 2", example_post_2.strip()),
        ("Example Post 3", example_post_3.strip()),
    ]
    profile.example_posts = "\n\n".join(
        f"--- {title} ---\n\n{text}" for title, text in examples if text
    )
    profile.updated_at = datetime.utcnow()
    profile.fingerprint_json = get_ai_client().fingerprint_voice(profile)
    db.commit()
    return RedirectResponse("/sources", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
def sources_page(
    request: Request,
    ai_error: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    sources = (
        db.query(Source)
        .filter(Source.workspace_id == workspace.id)
        .order_by(Source.created_at.desc())
        .all()
    )
    source_cards = [{"source": source, "summary": source_status_summary(source)} for source in sources]
    return templates.TemplateResponse(
        "sources.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "sources": sources,
            "source_cards": source_cards,
            "app_timezone": workspace.timezone_label or settings.app_timezone,
            "ai_status": ai_status(),
            "ai_error": ai_error,
            "active": "sources",
        },
    )


@app.post("/sources/paste")
def paste_source(
    title: str = Form(...),
    raw_text: str = Form(...),
    content_length: str = Form("medium"),
    emoji_style: str = Form("no_emojis"),
    post_structure: str = Form("hook_body_takeaway"),
    tone: str = Form("professional"),
    custom_tone: str = Form(""),
    content_goal: str = Form("educate"),
    post_language: str = Form("english"),
    post_tags: str = Form(""),
    extra_instructions: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    source = Source(
        workspace_id=workspace.id,
        title=title.strip(),
        source_type="paste",
        raw_text=raw_text.strip(),
        generation_preferences_json=build_source_preferences(
            content_length,
            emoji_style,
            post_structure,
            tone,
            custom_tone,
            content_goal,
            post_language,
            post_tags,
            extra_instructions,
        ),
        status="ready",
    )
    db.add(source)
    db.commit()
    return RedirectResponse(f"/sources/{source.id}", status_code=303)


@app.post("/sources/upload")
def upload_source(
    file: UploadFile = File(...),
    title: str = Form(""),
    content_length: str = Form("medium"),
    emoji_style: str = Form("no_emojis"),
    post_structure: str = Form("hook_body_takeaway"),
    tone: str = Form("professional"),
    custom_tone: str = Form(""),
    content_goal: str = Form("educate"),
    post_language: str = Form("english"),
    post_tags: str = Form(""),
    extra_instructions: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, TXT, and MD files are supported.")
    safe_name = f"{uuid.uuid4().hex}{suffix}"
    destination = settings.upload_dir / safe_name
    with destination.open("wb") as output:
        shutil.copyfileobj(file.file, output)
    try:
        raw_text = extract_text(destination)
    except UnsupportedFileType as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source = Source(
        workspace_id=workspace.id,
        title=title.strip() or Path(file.filename or "Uploaded source").stem,
        source_type="upload",
        raw_text=raw_text,
        file_path=str(destination),
        original_filename=file.filename,
        content_type=file.content_type,
        generation_preferences_json=build_source_preferences(
            content_length,
            emoji_style,
            post_structure,
            tone,
            custom_tone,
            content_goal,
            post_language,
            post_tags,
            extra_instructions,
        ),
        status="ready" if raw_text.strip() else "failed",
    )
    db.add(source)
    db.commit()
    return RedirectResponse(f"/sources/{source.id}", status_code=303)


@app.post("/campaigns/create")
def create_marketing_campaign(
    title: str = Form(...),
    campaign_type: str = Form("product"),
    brand_context: str = Form(...),
    target_audience: str = Form(...),
    key_message: str = Form(""),
    offer_details: str = Form(""),
    call_to_action: str = Form(""),
    style_samples: str = Form(""),
    campaign_arc: str = Form("launch_sequence"),
    post_count: int = Form(5),
    content_length: str = Form("medium"),
    emoji_style: str = Form("light_emojis"),
    post_structure: str = Form("hook_body_takeaway"),
    tone: str = Form("professional"),
    custom_tone: str = Form(""),
    content_goal: str = Form("promote_offer"),
    post_language: str = Form("english"),
    post_tags: str = Form(""),
    extra_instructions: str = Form(""),
    schedule_frequency: str = Form("daily"),
    schedule_every_days: int = Form(2),
    schedule_time_mode: str = Form("best"),
    schedule_custom_time: str = Form("09:00"),
    auto_schedule: str = Form("yes"),
    brand_logo: UploadFile | None = File(None),
    post_photo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    logo_path = save_optional_upload(brand_logo)
    photo_path = save_optional_upload(post_photo)
    post_count = min(max(1, post_count), 12)
    raw_text = build_campaign_brief(
        campaign_type,
        title.strip(),
        brand_context.strip(),
        target_audience.strip(),
        key_message.strip(),
        offer_details.strip(),
        call_to_action.strip(),
        logo_path,
        photo_path,
        style_samples.strip(),
    )
    source = Source(
        workspace_id=workspace.id,
        title=title.strip(),
        source_type="campaign",
        raw_text=raw_text,
        file_path=logo_path or photo_path or None,
        original_filename="Marketing campaign",
        generation_preferences_json=build_source_preferences(
            content_length,
            emoji_style,
            post_structure,
            tone,
            custom_tone,
            content_goal,
            post_language,
            post_tags,
            extra_instructions,
            campaign_arc,
        ),
        status="ready",
    )
    db.add(source)
    db.flush()
    insight = Insight(
        workspace_id=workspace.id,
        source_id=source.id,
        title=f"{campaign_type.replace('_', ' ').title()} campaign",
        insight_text=key_message.strip() or offer_details.strip() or brand_context.strip(),
        angle="marketing campaign",
        audience=target_audience.strip(),
        confidence_score=85,
    )
    db.add(insight)
    db.flush()
    profile = first_voice_profile(workspace, db)
    try:
        generated_posts = get_ai_client().generate_marketing_posts(source, profile, post_count)
    except Exception as exc:
        db.rollback()
        message = ai_error_message(exc)
        return RedirectResponse(f"/sources?ai_error={quote(message)}#add-source", status_code=303)
    group_id = uuid.uuid4().hex
    created_posts: list[Post] = []
    for index, draft_text in enumerate(generated_posts, start=1):
        post = Post(
            workspace_id=workspace.id,
            source_id=source.id,
            insight_id=insight.id,
            voice_profile_id=profile.id if profile else None,
            variant_group_id=group_id,
            variant_number=index,
            draft_text=draft_text,
            status="draft",
        )
        db.add(post)
        created_posts.append(post)
    db.flush()
    if auto_schedule == "yes":
        schedule_posts(created_posts, schedule_frequency, schedule_every_days, schedule_time_mode, schedule_custom_time)
    db.commit()
    return RedirectResponse("/calendar" if auto_schedule == "yes" else "/drafts", status_code=303)


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(
    source_id: int,
    request: Request,
    ai_error: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    source = db.get(Source, source_id)
    if not source or source.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    insights = (
        db.query(Insight)
        .filter(Insight.source_id == source.id)
        .order_by(Insight.created_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "source_detail.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "source": source,
            "source_preferences": source_preferences(source),
            "source_summary": source_status_summary(source),
            "insights": insights,
            "ai_status": ai_status(),
            "ai_error": ai_error,
            "active": "sources",
        },
    )


@app.post("/sources/{source_id}/extract")
def extract_insights(source_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    source = db.get(Source, source_id)
    if not source or source.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    profile = first_voice_profile(workspace, db)
    try:
        extracted = get_ai_client().extract_insights(source, profile)
    except Exception as exc:
        message = ai_error_message(exc)
        return RedirectResponse(f"/sources/{source.id}?ai_error={quote(message)}", status_code=303)
    for item in extracted:
        db.add(
            Insight(
                workspace_id=workspace.id,
                source_id=source.id,
                title=item.title,
                insight_text=item.insight_text,
                angle=item.angle,
                audience=item.audience,
                confidence_score=item.confidence_score,
            )
        )
    db.commit()
    return RedirectResponse(f"/sources/{source.id}", status_code=303)


@app.post("/sources/{source_id}/create-drafts")
def create_source_drafts(source_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    source = db.get(Source, source_id)
    if not source or source.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    profile = first_voice_profile(workspace, db)
    insights = (
        db.query(Insight)
        .filter(Insight.workspace_id == workspace.id, Insight.source_id == source.id)
        .order_by(Insight.created_at.asc())
        .all()
    )
    if not insights:
        try:
            extracted = get_ai_client().extract_insights(source, profile)
        except Exception as exc:
            message = ai_error_message(exc)
            return RedirectResponse(f"/sources/{source.id}?ai_error={quote(message)}", status_code=303)
        for item in extracted:
            db.add(
                Insight(
                    workspace_id=workspace.id,
                    source_id=source.id,
                    title=item.title,
                    insight_text=item.insight_text,
                    angle=item.angle,
                    audience=item.audience,
                    confidence_score=item.confidence_score,
                )
            )
        db.flush()
        insights = (
            db.query(Insight)
            .filter(Insight.workspace_id == workspace.id, Insight.source_id == source.id)
            .order_by(Insight.created_at.asc())
            .all()
        )
    try:
        for insight in insights:
            existing_posts = (
                db.query(Post)
                .filter(Post.workspace_id == workspace.id, Post.insight_id == insight.id)
                .first()
            )
            if existing_posts:
                continue
            create_posts_for_insight(db, insight, workspace, profile)
    except Exception as exc:
        db.rollback()
        message = ai_error_message(exc)
        return RedirectResponse(f"/sources/{source.id}?ai_error={quote(message)}", status_code=303)
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.post("/sources/{source_id}/delete")
def delete_source(source_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    source = db.get(Source, source_id)
    if not source or source.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    posts = db.query(Post).filter(Post.workspace_id == workspace.id, Post.source_id == source.id).all()
    insights = db.query(Insight).filter(Insight.workspace_id == workspace.id, Insight.source_id == source.id).all()
    for post in posts:
        db.delete(post)
    for insight in insights:
        db.delete(insight)
    file_path = source.file_path
    db.delete(source)
    db.commit()
    if file_path:
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse("/sources", status_code=303)


@app.post("/insights/{insight_id}/generate")
def generate_posts(insight_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    insight = db.get(Insight, insight_id)
    if not insight or insight.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    profile = first_voice_profile(workspace, db)
    try:
        create_posts_for_insight(db, insight, workspace, profile)
    except Exception as exc:
        message = ai_error_message(exc)
        return RedirectResponse(f"/sources/{insight.source_id}?ai_error={quote(message)}", status_code=303)
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.get("/drafts", response_class=HTMLResponse)
def drafts_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    posts = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id)
        .order_by(Post.created_at.desc(), Post.variant_number.asc())
        .all()
    )
    draft_groups = []
    grouped: dict[str, dict] = {}
    for post in posts:
        group = grouped.get(post.variant_group_id)
        if not group:
            group = {
                "id": post.variant_group_id,
                "created_at": post.created_at,
                "source": post.source,
                "insight": post.insight,
                "posts": [],
            }
            grouped[post.variant_group_id] = group
            draft_groups.append(group)
        group["posts"].append(post)
    schedulable_count = sum(1 for post in posts if post.status in {"draft", "approved", "ready_to_publish"} and not post.planned_publish_at)
    return templates.TemplateResponse(
        "drafts.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "posts": posts,
            "draft_groups": draft_groups,
            "schedulable_count": schedulable_count,
            "app_timezone": workspace.timezone_label or settings.app_timezone,
            "ai_status": ai_status(),
            "active": "drafts",
        },
    )


@app.post("/drafts/auto-schedule")
def auto_schedule_drafts(
    frequency: str = Form("daily"),
    every_days: int = Form(2),
    time_mode: str = Form("best"),
    custom_time: str = Form(""),
    ordering: str = Form("shuffle_sources"),
    scope: str = Form("ready"),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    if frequency != "every_n_days":
        every_days = 1
    if time_mode != "custom":
        custom_time = ""
    if ordering not in {"shuffle_sources", "source_sequence"}:
        ordering = "shuffle_sources"
    statuses = ["approved", "ready_to_publish"] if scope == "ready" else ["draft", "approved", "ready_to_publish"]
    posts = (
        db.query(Post)
        .filter(
            Post.workspace_id == workspace.id,
            Post.status.in_(statuses),
            Post.planned_publish_at.is_(None),
        )
        .order_by(Post.created_at.asc(), Post.variant_group_id.asc(), Post.variant_number.asc())
        .all()
    )
    ordered_posts = order_posts_for_auto_schedule(posts, ordering)
    publish_time = schedule_time_for_mode(time_mode, custom_time)
    publish_date = first_schedule_date(frequency, publish_time)
    for post in ordered_posts:
        post.status = "scheduled"
        post.planned_publish_at = datetime.combine(publish_date, publish_time)
        post.scheduled_at = datetime.utcnow()
        post.updated_at = datetime.utcnow()
        publish_date = next_schedule_date(publish_date, frequency, every_days)
    db.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.post("/drafts/{post_id}")
def update_draft(
    post_id: int,
    draft_text: str = Form(...),
    status: str = Form(...),
    planned_publish_at: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    post.draft_text = draft_text
    post.updated_at = datetime.utcnow()

    if planned_publish_at:
        post.status = "scheduled"
        try:
            post.planned_publish_at = datetime.fromisoformat(planned_publish_at)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid planned publish date.") from exc
        post.scheduled_at = datetime.utcnow()
    elif status == "scheduled":
        post.status = "approved"
        post.planned_publish_at = None
        post.scheduled_at = None
    else:
        post.status = status
        post.planned_publish_at = None
        post.scheduled_at = None
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.post("/drafts/{post_id}/delete")
def delete_draft(post_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    db.delete(post)
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.post("/drafts/groups/{group_id}/delete")
def delete_draft_group(group_id: str, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    posts = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.variant_group_id == group_id)
        .all()
    )
    if not posts:
        raise HTTPException(status_code=404)
    for post in posts:
        db.delete(post)
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.post("/publishing/run-due")
def publish_due_now(db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    publish_due_posts_once(workspace.id)
    return RedirectResponse("/calendar", status_code=303)


@app.post("/calendar/posts/{post_id}/reschedule")
def reschedule_calendar_post(
    post_id: int,
    draft_text: str = Form(...),
    planned_publish_at: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    try:
        planned_at = datetime.fromisoformat(planned_publish_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid planned publish date.") from exc
    post.draft_text = draft_text
    post.status = "scheduled"
    post.planned_publish_at = planned_at
    post.scheduled_at = datetime.utcnow()
    post.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.post("/calendar/posts/{post_id}/regenerate")
def regenerate_calendar_post(post_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    profile = first_voice_profile(workspace, db)
    try:
        new_text = get_ai_client().regenerate_post(post, profile)
        post.draft_text = new_text
        post.updated_at = datetime.utcnow()
        db.commit()
    except Exception:
        return RedirectResponse("/calendar?error=regenerate_failed", status_code=303)
    return RedirectResponse("/calendar", status_code=303)


@app.post("/calendar/posts/{post_id}/unschedule")
def unschedule_calendar_post(post_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    post.status = "ready_to_publish"
    post.planned_publish_at = None
    post.scheduled_at = None
    post.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/drafts", status_code=303)


@app.post("/calendar/posts/{post_id}/delete")
def delete_calendar_post(post_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    db.delete(post)
    db.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.post("/publishing/posts/{post_id}/retry")
def retry_publish(post_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    post = db.get(Post, post_id)
    if not post or post.workspace_id != workspace.id:
        raise HTTPException(status_code=404)
    if post.status != "publish_failed":
        raise HTTPException(status_code=400, detail="Only failed posts can be retried.")
    publish_post(db, post)
    db.commit()
    return RedirectResponse("/calendar", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    scheduled = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.status == "scheduled")
        .order_by(Post.planned_publish_at.asc())
        .all()
    )
    posted = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.status == "posted")
        .order_by(Post.posted_at.desc())
        .limit(10)
        .all()
    )
    failed = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.status == "publish_failed")
        .order_by(Post.updated_at.desc())
        .all()
    )
    scheduled_sources = sorted({post.source.title for post in scheduled if post.source and post.source.title})
    return templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "scheduled": scheduled,
            "scheduled_sources": scheduled_sources,
            "posted": posted,
            "failed": failed,
            "ai_status": ai_status(),
            "active": "calendar",
            "app_timezone": workspace.timezone_label or settings.app_timezone,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "linkedin_status": linkedin_status(workspace, db),
            "ai_status": ai_status(),
            "active": "settings",
            "app_timezone": workspace.timezone_label or settings.app_timezone,
            "common_timezones": COMMON_TIMEZONE_LABELS,
        },
    )


@app.post("/settings")
def update_settings(
    request: Request,
    workspace_name: str = Form(...),
    timezone_label: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    workspace = current_workspace(user, db)
    workspace.name = workspace_name.strip() or workspace.name
    workspace.timezone_label = timezone_label.strip()
    db.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/history", response_class=HTMLResponse)
def history_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    workspace = current_workspace(user, db)
    posted = (
        db.query(Post)
        .filter(Post.workspace_id == workspace.id, Post.status == "posted")
        .order_by(Post.posted_at.desc(), Post.updated_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        "history.html",
        {
            "request": request,
            "user": user,
            "workspace": workspace,
            "posted": posted,
            "ai_status": ai_status(),
            "active": "history",
            "app_timezone": workspace.timezone_label or settings.app_timezone,
        },
    )


