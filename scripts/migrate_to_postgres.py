"""One-time data migration: local SQLite (data/tacit.db) -> Postgres (DATABASE_URL).

Usage:
    1. Set DATABASE_URL in .env to your Supabase connection string
       (postgresql+psycopg://postgres:[PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres)
    2. Run: .venv/Scripts/python.exe scripts/migrate_to_postgres.py
    3. Safe to re-run - uses upsert-by-primary-key, won't duplicate rows.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import (
    Base,
    Insight,
    LinkedInAccount,
    PlatformSettings,
    Post,
    Source,
    Testimonial,
    User,
    VoiceProfile,
    Workspace,
    WorkspaceMember,
)

SOURCE_URL = "sqlite:///./data/tacit.db"
MODELS_IN_FK_ORDER = [
    User,
    Workspace,
    WorkspaceMember,
    VoiceProfile,
    Source,
    Insight,
    Post,
    LinkedInAccount,
    PlatformSettings,
    Testimonial,
]


def main() -> None:
    target_url = settings.database_url
    if target_url.startswith("sqlite"):
        print("DATABASE_URL is still pointing at SQLite. Set it to your Supabase Postgres URL first.")
        sys.exit(1)

    print(f"Source: {SOURCE_URL}")
    print(f"Target: {target_url.split('@')[-1] if '@' in target_url else target_url}")

    source_engine = create_engine(SOURCE_URL, connect_args={"check_same_thread": False})
    target_engine = create_engine(target_url)

    Base.metadata.create_all(bind=target_engine)

    SourceSession = sessionmaker(bind=source_engine)
    TargetSession = sessionmaker(bind=target_engine)
    src = SourceSession()
    dst = TargetSession()

    for model in MODELS_IN_FK_ORDER:
        rows = src.query(model).all()
        print(f"Migrating {len(rows)} rows from {model.__tablename__}...")
        for row in rows:
            data = {c.name: getattr(row, c.name) for c in model.__table__.columns}
            dst.merge(model(**data))
        dst.commit()

        with target_engine.begin() as conn:
            conn.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence('{model.__tablename__}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {model.__tablename__}), 1))"
                )
            )

    src.close()
    dst.close()
    print("Migration complete.")


if __name__ == "__main__":
    main()
