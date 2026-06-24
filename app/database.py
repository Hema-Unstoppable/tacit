from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    db_path = settings.database_url.replace("sqlite:///", "", 1)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    ensure_lightweight_migrations()


def ensure_lightweight_migrations() -> None:
    inspector = inspect(engine)
    if "sources" not in inspector.get_table_names():
        return
    source_columns = {column["name"] for column in inspector.get_columns("sources")}
    post_columns = {column["name"] for column in inspector.get_columns("posts")} if "posts" in inspector.get_table_names() else set()
    linkedin_columns = {column["name"] for column in inspector.get_columns("linkedin_accounts")} if "linkedin_accounts" in inspector.get_table_names() else set()
    workspace_columns = {column["name"] for column in inspector.get_columns("workspaces")} if "workspaces" in inspector.get_table_names() else set()
    with engine.begin() as connection:
        if "generation_preferences_json" not in source_columns:
            connection.execute(text("ALTER TABLE sources ADD COLUMN generation_preferences_json TEXT DEFAULT ''"))
        if "posted_at" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN posted_at TIMESTAMP"))
        if "linkedin_post_id" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN linkedin_post_id VARCHAR(255) DEFAULT ''"))
        if "publish_error" not in post_columns:
            connection.execute(text("ALTER TABLE posts ADD COLUMN publish_error TEXT DEFAULT ''"))
        if "org_urn" not in linkedin_columns:
            connection.execute(text("ALTER TABLE linkedin_accounts ADD COLUMN org_urn VARCHAR(255) DEFAULT ''"))
        if "org_name" not in linkedin_columns:
            connection.execute(text("ALTER TABLE linkedin_accounts ADD COLUMN org_name VARCHAR(255) DEFAULT ''"))
        user_columns = {column["name"] for column in inspector.get_columns("users")} if "users" in inspector.get_table_names() else set()
        if "email_verified" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN email_verified INTEGER DEFAULT 1"))
        if "google_id" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN google_id VARCHAR(255)"))
        if "name" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN name VARCHAR(255) DEFAULT ''"))
        if "picture_url" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN picture_url VARCHAR(1024) DEFAULT ''"))
        if "onboarding_complete" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN onboarding_complete INTEGER DEFAULT 1"))
        if "is_admin" not in user_columns:
            connection.execute(text("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0"))
            connection.execute(text("UPDATE users SET is_admin = 1 WHERE email = 'hema.bhit@gmail.com'"))
        if "timezone_label" not in workspace_columns:
            connection.execute(text("ALTER TABLE workspaces ADD COLUMN timezone_label VARCHAR(64) DEFAULT ''"))
        if "testimonial_prompted" not in workspace_columns:
            connection.execute(text("ALTER TABLE workspaces ADD COLUMN testimonial_prompted INTEGER DEFAULT 0"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
